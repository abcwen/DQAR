import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR
from utils.logger import logger

from agents.diffusion import Diffusion
from agents.model import MLP
from agents.helpers import EMA


class Critic(nn.Module):
    def __init__(self, state_dim, action_dim, hidden_dim=256, num_q_networks=5):
        super(Critic, self).__init__()

        self.q_networks = nn.ModuleList([
            nn.Sequential(
                nn.Linear(state_dim + action_dim, hidden_dim),
                nn.Mish(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.Mish(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.Mish(),
                nn.Linear(hidden_dim, 1)
            ) for _ in range(num_q_networks)
        ])

    def forward(self, state, action):
        x = torch.cat([state, action], dim=-1)
        q_values = [q_network(x) for q_network in self.q_networks]
        return torch.cat(q_values, dim=-1)

    def q_mean_var(self, state, action):
        q_values = self.forward(state, action)
        q_mean = torch.mean(q_values, dim=-1, keepdim=True)
        q_std = torch.std(q_values, dim=-1, keepdim=True, unbiased=True)
        return q_mean, q_std

    def q1(self, state, action):
        x = torch.cat([state, action], dim=-1)
        return self.q1_model(x)

    def q_min(self, state, action):
        q_values = self.forward(state, action)
        # q1, q2 = self.forward(state, action)
        return torch.min(q_values, dim=-1, keepdim=True)[0]


class Diffusion_QL(object):
    def __init__(self,
                 state_dim,
                 action_dim,
                 max_action,
                 device,
                 discount,
                 tau,
                 max_q_backup=False,
                 eta=1.0,
                 beta_schedule='linear',
                 n_timesteps=100,
                 ema_decay=0.995,
                 step_start_ema=1000,
                 update_ema_every=5,
                 lr=3e-4,
                 lr_decay=False,
                 lr_maxt=1000,
                 grad_norm=1.0,
                 num_ensemble=5,
                 advantage_threshold=0.0,
                 lambda_adv=2.5
                 ):

        self.model = MLP(state_dim=state_dim, action_dim=action_dim, device=device)

        self.actor = Diffusion(state_dim=state_dim, action_dim=action_dim, model=self.model, max_action=max_action,
                               beta_schedule=beta_schedule, n_timesteps=n_timesteps,).to(device)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr)

        self.lr_decay = lr_decay
        self.grad_norm = grad_norm

        self.step = 0
        self.step_start_ema = step_start_ema
        self.ema = EMA(ema_decay)
        self.ema_model = copy.deepcopy(self.actor)
        self.update_ema_every = update_ema_every

        self.critic = Critic(state_dim, action_dim, num_q_networks=num_ensemble).to(device)
        self.critic_target = copy.deepcopy(self.critic)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=3e-4)

        if lr_decay:
            self.actor_lr_scheduler = CosineAnnealingLR(self.actor_optimizer, T_max=lr_maxt, eta_min=0.)
            self.critic_lr_scheduler = CosineAnnealingLR(self.critic_optimizer, T_max=lr_maxt, eta_min=0.)

        self.state_dim = state_dim
        self.max_action = max_action
        self.action_dim = action_dim
        self.discount = discount
        self.tau = tau
        self.eta = eta  # q_learning weight
        self.device = device
        self.max_q_backup = max_q_backup
        self.num_ensemble = num_ensemble

        self.ema_std = 0.0
        self.ema_decay = 0.9
        self.ema_initialized = False

    def step_ema(self):
        if self.step < self.step_start_ema:
            return
        self.ema.update_model_average(self.ema_model, self.actor)

    def train(self, replay_buffer, iterations, batch_size=100, log_writer=None):

        metric = {'bc_loss': [], 'ql_loss': [], 'actor_loss': [], 'critic_loss': [], 'value_loss': []}
        for _ in range(iterations):
            # Sample replay buffer / batch
            state, action, next_state, reward, not_done = replay_buffer.sample(batch_size)

            """ Q Training """
            current_q_values = self.critic(state, action)

            if self.max_q_backup:
                next_state_rpt = torch.repeat_interleave(next_state, repeats=10, dim=0)
                next_action_rpt = self.ema_model(next_state_rpt)
                next_q_values = self.critic_target(next_state_rpt, next_action_rpt)

                next_q_mean = next_q_values.mean(dim=-1, keepdim=True)
                next_q_std = torch.std(next_q_values, dim=-1, unbiased=False, keepdim=True)
                target_q = next_q_mean - next_q_std  

                # target_q, _ = torch.min(next_q_values, dim=-1, keepdim=True)
                # target_q = next_q_values.mean(dim=-1, keepdim=True)

                target_q = target_q.view(batch_size, 10).max(dim=1, keepdim=True)[0]

            else:
                next_action = self.ema_model(next_state)
                next_q_values = self.critic_target(next_state, next_action)

                next_q_mean = next_q_values.mean(dim=-1, keepdim=True)
                next_q_std = torch.std(next_q_values, dim=-1, unbiased=False, keepdim=True)
                target_q = next_q_mean - next_q_std

            target_q = (reward + not_done * self.discount * target_q).detach()
            # critic_loss.shape = (batch_size, 1)
            critic_loss = F.mse_loss(current_q_values, target_q.expand_as(current_q_values))
            self.critic_optimizer.zero_grad()
            critic_loss.backward()
            if self.grad_norm > 0:
                critic_grad_norms = nn.utils.clip_grad_norm_(self.critic.parameters(), max_norm=self.grad_norm, norm_type=2)
            self.critic_optimizer.step()

            """ Policy Training """
            bc_loss = self.actor.loss(action, state)

            new_action = self.actor(state)
            q_values_new_action = self.critic(state, new_action)
            batch_size, num_q_networks = q_values_new_action.shape

            q_std = torch.std(q_values_new_action, dim=-1, unbiased=False, keepdim=True)
            eta = 1.0 / torch.exp(0.5 * q_std.mean().detach())
            eta = torch.clamp(eta, min=0.1)

            idx1 = torch.randint(0, num_q_networks, (batch_size, 1), device=q_values_new_action.device)
            offset = torch.randint(1, num_q_networks, (batch_size, 1), device=q_values_new_action.device)
            idx2 = (idx1 + offset) % num_q_networks

            q1_new_action = torch.gather(q_values_new_action, 1, idx1)
            q2_new_action = torch.gather(q_values_new_action, 1, idx2)
            if np.random.uniform() > 0.5:
                q_loss = - q1_new_action.mean() / q2_new_action.abs().mean().detach()
                alpha_weight = 1 / q2_new_action.abs().mean().detach()
            else:
                q_loss = - q2_new_action.mean() / q1_new_action.abs().mean().detach()
                alpha_weight = 1 / q1_new_action.abs().mean().detach()
            actor_loss = eta * bc_loss + q_loss

            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            if self.grad_norm > 0:
                actor_grad_norms = nn.utils.clip_grad_norm_(self.actor.parameters(), max_norm=self.grad_norm, norm_type=2)
            self.actor_optimizer.step()


            """ Step Target network """
            if self.step % self.update_ema_every == 0:
                self.step_ema()

            for param, target_param in zip(self.critic.parameters(), self.critic_target.parameters()):
                target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

            self.step += 1

            """ Log """
            if log_writer is not None:
                if self.grad_norm > 0:
                    log_writer.add_scalar('Actor Grad Norm', actor_grad_norms.max().item(), self.step)
                    log_writer.add_scalar('Critic Grad Norm', critic_grad_norms.max().item(), self.step)
                log_writer.add_scalar('BC Loss', bc_loss.item(), self.step)
                log_writer.add_scalar('QL Loss', q_loss.item(), self.step)
                log_writer.add_scalar('Critic Loss', critic_loss.item(), self.step)
                log_writer.add_scalar('Target_Q Mean', target_q.mean().item(), self.step)

            metric['actor_loss'].append(actor_loss.item())
            metric['bc_loss'].append(bc_loss.item())
            metric['ql_loss'].append(q_loss.item())
            metric['critic_loss'].append(critic_loss.item())

        if self.lr_decay: 
            self.actor_lr_scheduler.step()
            self.critic_lr_scheduler.step()

        return metric

    def evaluate_q_values(self, env, num_evaluations=10, num_steps=5000, num_seeds=5):
        q_values = []
        true_q_values = []
        for seed in range(num_seeds):
            env.seed(seed)
            state = env.reset()
            for _ in range(num_evaluations):
                states = []
                for _ in range(10):
                    state = env.reset()
                    states.append(state)
                states = torch.tensor(states, dtype=torch.float32).to(self.device)
                actions = self.actor(states)
                q_values_batch = self.critic(states, actions)
                q_values.extend(q_values_batch.cpu().detach().numpy())
                true_q_batch = []
                for state, action in zip(states, actions):
                    env_state = state.cpu().numpy()
                    env.reset()
                    env.state = env_state
                    total_reward = 0
                    done = False
                    for _ in range(num_steps):
                        if done:
                            break
                        action_np = action.cpu().numpy()
                        next_state, reward, done, _ = env.step(action_np)
                        total_reward += reward * self.discount ** _
                    true_q_batch.append(total_reward)
                true_q_values.extend(true_q_batch)
        return np.array(q_values), np.array(true_q_values)

    def sample_action(self, state):
        state = torch.FloatTensor(state.reshape(1, -1)).to(self.device)
        state_rpt = torch.repeat_interleave(state, repeats=50, dim=0)
        with torch.no_grad():
            action = self.actor.sample(state_rpt)
            q_mean, q_std = self.critic_target.q_mean_var(state_rpt, action)
            q_value = q_mean.flatten()
            idx = torch.multinomial(F.softmax(q_value), 1)
        return action[idx].cpu().data.numpy().flatten()

    def save_model(self, dir, id=None):
        if id is not None:
            torch.save(self.actor.state_dict(), f'{dir}/actor_{id}.pth')
            torch.save(self.critic.state_dict(), f'{dir}/critic_{id}.pth')
        else:
            torch.save(self.actor.state_dict(), f'{dir}/actor.pth')
            torch.save(self.critic.state_dict(), f'{dir}/critic.pth')

    def load_model(self, dir, id=None):
        if id is not None:
            self.actor.load_state_dict(torch.load(f'{dir}/actor_{id}.pth'))
            self.critic.load_state_dict(torch.load(f'{dir}/critic_{id}.pth'))
        else:
            self.actor.load_state_dict(torch.load(f'{dir}/actor.pth'))
            self.critic.load_state_dict(torch.load(f'{dir}/critic.pth'))


