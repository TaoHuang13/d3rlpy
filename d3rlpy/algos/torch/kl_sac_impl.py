import copy
import math
from typing import Optional, Sequence, Tuple

import numpy as np
import torch
from torch.optim import Optimizer

from ...gpu import Device
from ...models.builders import (
    create_categorical_policy,
    create_discrete_q_function,
    create_parameter,
    create_squashed_normal_policy,
)
from ...models.encoders import EncoderFactory
from ...models.optimizers import OptimizerFactory
from ...models.q_functions import QFunctionFactory
from ...models.torch import (
    CategoricalPolicy,
    EnsembleDiscreteQFunction,
    EnsembleQFunction,
    Parameter,
    Policy,
    SquashedNormalPolicy,
)
from ...preprocessing import ActionScaler, RewardScaler, Scaler
from ...torch_utility import TorchMiniBatch, hard_sync, torch_api, train_api
from .base import TorchImplBase
from .ddpg_impl import DDPGBaseImpl
from .utility import DiscreteQFunctionMixin


class KlSACImpl(DDPGBaseImpl):

    _policy: Optional[SquashedNormalPolicy]
    _targ_policy: Optional[SquashedNormalPolicy]
    _temp_learning_rate: float
    _temp_optim_factory: OptimizerFactory
    _initial_temperature: float
    _log_temp: Optional[Parameter]
    _temp_optim: Optional[Optimizer]

    def __init__(
        self,
        observation_shape: Sequence[int],
        action_size: int,
        actor_learning_rate: float,
        critic_learning_rate: float,
        temp_learning_rate: float,
        actor_optim_factory: OptimizerFactory,
        critic_optim_factory: OptimizerFactory,
        temp_optim_factory: OptimizerFactory,
        actor_encoder_factory: EncoderFactory,
        critic_encoder_factory: EncoderFactory,
        q_func_factory: QFunctionFactory,
        gamma: float,
        tau: float,
        n_critics: int,
        initial_temperature: float,
        use_gpu: Optional[Device],
        scaler: Optional[Scaler],
        action_scaler: Optional[ActionScaler],
        reward_scaler: Optional[RewardScaler],
        # prior policy
        prior_policy,
        target_kl
    ):
        super().__init__(
            observation_shape=observation_shape,
            action_size=action_size,
            actor_learning_rate=actor_learning_rate,
            critic_learning_rate=critic_learning_rate,
            actor_optim_factory=actor_optim_factory,
            critic_optim_factory=critic_optim_factory,
            actor_encoder_factory=actor_encoder_factory,
            critic_encoder_factory=critic_encoder_factory,
            q_func_factory=q_func_factory,
            gamma=gamma,
            tau=tau,
            n_critics=n_critics,
            use_gpu=use_gpu,
            scaler=scaler,
            action_scaler=action_scaler,
            reward_scaler=reward_scaler,
        )
        self._temp_learning_rate = temp_learning_rate
        self._temp_optim_factory = temp_optim_factory
        self._initial_temperature = initial_temperature

        # initialized in build
        self._log_temp = None
        self._temp_optim = None

        # prior policy
        self._prior = prior_policy
        self.target_kl = target_kl

    def build(self) -> None:
        self._build_temperature()
        self._build_prior_weight()
        super().build()
        self._build_temperature_optim()
        self._build_prior_weight_optim()

    def _build_prior_weight(self) -> None:
        self._prior_weight = MLP(self._observation_shape[0], 1)

    def _build_prior_weight_optim(self) -> None:
        self._prior_weight_optim = self._temp_optim_factory.create(
            self._prior_weight.parameters(), lr=1e-4
        )

    def _build_actor(self) -> None:
        self._policy = create_squashed_normal_policy(
            self._observation_shape,
            self._action_size,
            self._actor_encoder_factory,
        )

    def _build_temperature(self) -> None:
        initial_val = math.log(self._initial_temperature) 
        #initial_val = math.log(0.5)    # TODO: 
        self._log_temp = create_parameter((1, 1), initial_val)

    def _build_temperature_optim(self) -> None:
        assert self._log_temp is not None
        self._temp_optim = self._temp_optim_factory.create(
            self._log_temp.parameters(), lr=self._temp_learning_rate
        )

    def compute_actor_loss(self, batch: TorchMiniBatch) -> torch.Tensor:
        assert self._policy is not None
        assert self._log_temp is not None
        assert self._q_func is not None
        assert self._prior is not None
        action, log_prob = self._policy.sample_with_log_prob(batch.observations)
        entropy = self._log_temp().exp() * log_prob 

        # prior entropy
        policy_dist = self._policy.dist(batch.observations)
        prior_dist = self._prior.dist(batch.observations)
        prior_weight = self._prior_weight(batch.observations).detach()
        kl_div = self.mc_kl_divergence(policy_dist, prior_dist, prior_weight, n_samples=10) * self._log_temp().exp()

        q_t = self._q_func(batch.observations, action, "min")
        return (kl_div - q_t).mean()

        prior_log_prob = self._prior.compute_log_prob(batch.observations, action).detach()
        prior_prob = prior_log_prob.exp()

        uniform_prob =  torch.ones_like(prior_log_prob) * 0.5   # assume [-1, 1] action range

        prior_weight = self._prior_weight(batch.observations).detach()
        mixed_prob = prior_weight * prior_prob + (1 - prior_weight) * uniform_prob + 1e-10
        mixed_log_prob = torch.log(mixed_prob) * self._log_temp().exp() 

        q_t = self._q_func(batch.observations, action, "min")
        return (entropy - q_t - mixed_log_prob).mean()

    def mc_kl_divergence(self, policy_dist, prior_dist, prior_weight, n_samples=1):
        samples = [policy_dist.sample() for _ in range(n_samples)]
        kl_div = []
        for x in samples:
            policy_log_prob = policy_dist.log_prob(x)
            prior_log_prob = prior_dist.log_prob(x)
            prior_prob = prior_log_prob.exp()
            uniform_prob = torch.ones_like(prior_log_prob) * 0.5
            mixed_prob = prior_weight * prior_prob + (1 - prior_weight) * uniform_prob + 1e-6
            mixed_log_prob = torch.log(mixed_prob)
            kl_div.append(policy_log_prob - mixed_log_prob)
        
        kl_div = torch.stack(kl_div, dim=1).mean(dim=1)
        return kl_div

    def compute_prior_weight_loss(self, batch: TorchMiniBatch) -> torch.Tensor:
        q_mixes = []
        for i in range(1):
            prior_action = self._prior(batch.observations, deterministic=False, with_log_prob=False).detach()
            uniform_action = torch.rand(prior_action.size()) * 2 - 1
            prior_weight = self._prior_weight(batch.observations)
            mixed_action = prior_weight * prior_action + (1 - prior_weight) * uniform_action
            q_mix = self._q_func(batch.observations, mixed_action, "min")
            q_mixes.append(q_mix)


        return - torch.stack(q_mixes).mean()

    @train_api
    @torch_api()
    def update_temp(
        self, batch: TorchMiniBatch
    ) -> Tuple[np.ndarray, np.ndarray]:
        assert self._temp_optim is not None
        assert self._policy is not None
        assert self._log_temp is not None

        # self._temp_optim.zero_grad()

        # with torch.no_grad():
        #     _, log_prob = self._policy.sample_with_log_prob(batch.observations)
        #     targ_temp = log_prob - self._action_size

        # loss = -(self._log_temp().exp() * targ_temp).mean()

        # loss.backward()
        # self._temp_optim.step()

        # # current temperature value
        # cur_temp = self._log_temp().exp().cpu().detach().numpy()[0][0]

        # return loss.cpu().detach().numpy(), cur_temp

        self._temp_optim.zero_grad()
        with torch.no_grad():
            policy_dist = self._policy.dist(batch.observations)
            prior_dist = self._prior.dist(batch.observations)
            prior_weight = self._prior_weight(batch.observations).detach()
            kl_div = self.mc_kl_divergence(policy_dist, prior_dist, prior_weight)
        
        loss = (self._log_temp().exp() * (self.target_kl - kl_div).detach()).mean()

        loss.backward()
        self._temp_optim.step()

        # current temperature value
        cur_temp = self._log_temp().exp().cpu().detach().numpy()[0][0]

        return loss.cpu().detach().numpy(), cur_temp

    @train_api
    @torch_api()
    def update_prior_weight(self, batch: TorchMiniBatch) -> np.ndarray:
        assert self._prior_weight_optim is not None

        self._prior_weight_optim.zero_grad()

        loss = self.compute_prior_weight_loss(batch)

        loss.backward()
        self._prior_weight_optim.step()

        return loss.cpu().detach().numpy()

    def compute_target(self, batch: TorchMiniBatch) -> torch.Tensor:
        assert self._policy is not None
        assert self._log_temp is not None
        assert self._targ_q_func is not None
        with torch.no_grad():
            action, log_prob = self._policy.sample_with_log_prob(
                batch.next_observations
            )
            entropy = self._log_temp().exp() * log_prob
            target = self._targ_q_func.compute_target(
                batch.next_observations,
                action,
                reduction="min",
            )

            # prior_log_prob = self._prior.compute_log_prob(batch.next_observations, action).detach()
            # prior_prob = prior_log_prob.exp()

            # uniform_prob =  torch.ones_like(prior_log_prob) * 0.5   # assume [-1, 1] action range

            # prior_weight = self._prior_weight(batch.next_observations).detach()

            # mixed_prob = prior_weight * prior_prob + (1 - prior_weight) * uniform_prob + 1e-10
            # mixed_log_prob = torch.log(mixed_prob) * self._log_temp().exp()

            policy_dist = self._policy.dist(batch.next_observations)
            prior_dist = self._prior.dist(batch.next_observations)
            prior_weight = self._prior_weight(batch.next_observations).detach()
            kl_div = self.mc_kl_divergence(policy_dist, prior_dist, prior_weight, n_samples=10) * self._log_temp().exp()

            return target - kl_div

            return target - entropy + mixed_log_prob


class DiscreteSACImpl(DiscreteQFunctionMixin, TorchImplBase):

    _actor_learning_rate: float
    _critic_learning_rate: float
    _temp_learning_rate: float
    _actor_optim_factory: OptimizerFactory
    _critic_optim_factory: OptimizerFactory
    _temp_optim_factory: OptimizerFactory
    _actor_encoder_factory: EncoderFactory
    _critic_encoder_factory: EncoderFactory
    _q_func_factory: QFunctionFactory
    _gamma: float
    _n_critics: int
    _initial_temperature: float
    _use_gpu: Optional[Device]
    _policy: Optional[CategoricalPolicy]
    _q_func: Optional[EnsembleDiscreteQFunction]
    _targ_q_func: Optional[EnsembleDiscreteQFunction]
    _log_temp: Optional[Parameter]
    _actor_optim: Optional[Optimizer]
    _critic_optim: Optional[Optimizer]
    _temp_optim: Optional[Optimizer]

    def __init__(
        self,
        observation_shape: Sequence[int],
        action_size: int,
        actor_learning_rate: float,
        critic_learning_rate: float,
        temp_learning_rate: float,
        actor_optim_factory: OptimizerFactory,
        critic_optim_factory: OptimizerFactory,
        temp_optim_factory: OptimizerFactory,
        actor_encoder_factory: EncoderFactory,
        critic_encoder_factory: EncoderFactory,
        q_func_factory: QFunctionFactory,
        gamma: float,
        n_critics: int,
        initial_temperature: float,
        use_gpu: Optional[Device],
        scaler: Optional[Scaler],
        reward_scaler: Optional[RewardScaler],
    ):
        super().__init__(
            observation_shape=observation_shape,
            action_size=action_size,
            scaler=scaler,
            action_scaler=None,
            reward_scaler=reward_scaler,
        )
        self._actor_learning_rate = actor_learning_rate
        self._critic_learning_rate = critic_learning_rate
        self._temp_learning_rate = temp_learning_rate
        self._actor_optim_factory = actor_optim_factory
        self._critic_optim_factory = critic_optim_factory
        self._temp_optim_factory = temp_optim_factory
        self._actor_encoder_factory = actor_encoder_factory
        self._critic_encoder_factory = critic_encoder_factory
        self._q_func_factory = q_func_factory
        self._gamma = gamma
        self._n_critics = n_critics
        self._initial_temperature = initial_temperature
        self._use_gpu = use_gpu

        # initialized in build
        self._q_func = None
        self._policy = None
        self._targ_q_func = None
        self._log_temp = None
        self._actor_optim = None
        self._critic_optim = None
        self._temp_optim = None

    def build(self) -> None:
        self._build_critic()
        self._build_actor()
        self._build_temperature()

        # setup target networks
        self._targ_q_func = copy.deepcopy(self._q_func)

        if self._use_gpu:
            self.to_gpu(self._use_gpu)
        else:
            self.to_cpu()

        # setup optimizer after the parameters move to GPU
        self._build_critic_optim()
        self._build_actor_optim()
        self._build_temperature_optim()

    def _build_critic(self) -> None:
        self._q_func = create_discrete_q_function(
            self._observation_shape,
            self._action_size,
            self._critic_encoder_factory,
            self._q_func_factory,
            n_ensembles=self._n_critics,
        )

    def _build_critic_optim(self) -> None:
        assert self._q_func is not None
        self._critic_optim = self._critic_optim_factory.create(
            self._q_func.parameters(), lr=self._critic_learning_rate
        )

    def _build_actor(self) -> None:
        self._policy = create_categorical_policy(
            self._observation_shape,
            self._action_size,
            self._actor_encoder_factory,
        )

    def _build_actor_optim(self) -> None:
        assert self._policy is not None
        self._actor_optim = self._actor_optim_factory.create(
            self._policy.parameters(), lr=self._actor_learning_rate
        )

    def _build_temperature(self) -> None:
        initial_val = math.log(self._initial_temperature)
        self._log_temp = create_parameter((1, 1), initial_val)

    def _build_temperature_optim(self) -> None:
        assert self._log_temp is not None
        self._temp_optim = self._temp_optim_factory.create(
            self._log_temp.parameters(), lr=self._temp_learning_rate
        )

    @train_api
    @torch_api()
    def update_critic(self, batch: TorchMiniBatch) -> np.ndarray:
        assert self._critic_optim is not None

        self._critic_optim.zero_grad()

        q_tpn = self.compute_target(batch)
        loss = self.compute_critic_loss(batch, q_tpn)

        loss.backward()
        self._critic_optim.step()

        return loss.cpu().detach().numpy()

    def compute_target(self, batch: TorchMiniBatch) -> torch.Tensor:
        assert self._policy is not None
        assert self._log_temp is not None
        assert self._targ_q_func is not None
        with torch.no_grad():
            log_probs = self._policy.log_probs(batch.next_observations)
            probs = log_probs.exp()
            entropy = self._log_temp().exp() * log_probs
            target = self._targ_q_func.compute_target(batch.next_observations)
            keepdims = True
            if target.dim() == 3:
                entropy = entropy.unsqueeze(-1)
                probs = probs.unsqueeze(-1)
                keepdims = False
            return (probs * (target - entropy)).sum(dim=1, keepdim=keepdims)

    def compute_critic_loss(
        self,
        batch: TorchMiniBatch,
        q_tpn: torch.Tensor,
    ) -> torch.Tensor:
        assert self._q_func is not None
        return self._q_func.compute_error(
            observations=batch.observations,
            actions=batch.actions.long(),
            rewards=batch.rewards,
            target=q_tpn,
            terminals=batch.terminals,
            gamma=self._gamma**batch.n_steps,
        )

    @train_api
    @torch_api()
    def update_actor(self, batch: TorchMiniBatch) -> np.ndarray:
        assert self._q_func is not None
        assert self._actor_optim is not None

        # Q function should be inference mode for stability
        self._q_func.eval()

        self._actor_optim.zero_grad()

        loss = self.compute_actor_loss(batch)

        loss.backward()
        self._actor_optim.step()

        return loss.cpu().detach().numpy()

    def compute_actor_loss(self, batch: TorchMiniBatch) -> torch.Tensor:
        assert self._q_func is not None
        assert self._policy is not None
        assert self._log_temp is not None
        with torch.no_grad():
            q_t = self._q_func(batch.observations, reduction="min")
        log_probs = self._policy.log_probs(batch.observations)
        probs = log_probs.exp()
        entropy = self._log_temp().exp() * log_probs
        return (probs * (entropy - q_t)).sum(dim=1).mean()

    @train_api
    @torch_api()
    def update_temp(self, batch: TorchMiniBatch) -> np.ndarray:
        assert self._temp_optim is not None
        assert self._policy is not None
        assert self._log_temp is not None

        self._temp_optim.zero_grad()

        with torch.no_grad():
            log_probs = self._policy.log_probs(batch.observations)
            probs = log_probs.exp()
            expct_log_probs = (probs * log_probs).sum(dim=1, keepdim=True)
            entropy_target = 0.98 * (-math.log(1 / self.action_size))
            targ_temp = expct_log_probs + entropy_target

        loss = -(self._log_temp().exp() * targ_temp).mean()

        loss.backward()
        self._temp_optim.step()

        # current temperature value
        cur_temp = self._log_temp().exp().cpu().detach().numpy()[0][0]

        return loss.cpu().detach().numpy(), cur_temp

    def _predict_best_action(self, x: torch.Tensor) -> torch.Tensor:
        assert self._policy is not None
        return self._policy.best_action(x)

    def _sample_action(self, x: torch.Tensor) -> torch.Tensor:
        assert self._policy is not None
        return self._policy.sample(x)

    def update_target(self) -> None:
        assert self._q_func is not None
        assert self._targ_q_func is not None
        hard_sync(self._targ_q_func, self._q_func)

    @property
    def policy(self) -> Policy:
        assert self._policy
        return self._policy

    @property
    def policy_optim(self) -> Optimizer:
        assert self._actor_optim
        return self._actor_optim

    @property
    def q_function(self) -> EnsembleQFunction:
        assert self._q_func
        return self._q_func

    @property
    def q_function_optim(self) -> Optimizer:
        assert self._critic_optim
        return self._critic_optim


import torch.nn as nn


class MLP(nn.Module):
    def __init__(self, in_dim, out_dim, hidden_size=256) -> None:
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(in_dim, hidden_size), nn.ReLU(),
            nn.Linear(hidden_size, hidden_size), nn.ReLU(),
            nn.Linear(hidden_size, out_dim), nn.Sigmoid()
        )

    def forward(self, s):
        logits = self.mlp(s)
        return logits
    