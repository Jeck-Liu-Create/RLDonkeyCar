import random
import time
import os

from collections import deque

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
from torch.utils.data import DataLoader

import matplotlib.pyplot as plt

import cv2
import numpy as np

from .modules import MLP
from .ae import AE 

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)

class Actor(nn.Module):
    """ Gaussian Policy """
    def __init__(self, input_size, act_size, hidden_size):
        super().__init__()
        self.act_size = act_size
        self.net = MLP(input_size, act_size * 2, hidden_size)
        
    def forward(self, state):
        x = self.net(state)
        mean, log_std = x[:, :self.act_size], x[:, self.act_size:]

        log_std = torch.clamp(log_std, min=-20, max=2)
        return mean, log_std

    def sample(self, state):
        mean, log_std = self.forward(state)
        normal = Normal(mean, log_std.exp())
        x = normal.rsample()

        # Enforcing action bounds
        action = torch.tanh(x)
        log_prob = normal.log_prob(x) - torch.log(1 - action**2 + 1e-6)
        log_prob = log_prob.sum(1, keepdim=True)
        return action, log_prob

    def select_action(self, state):
    
        action, _ = self.sample(state)
        
        return action[0].detach().cpu().numpy()

class Critic(nn.Module):
    """ Twin Q-networks """
    def __init__(self, input_size, hidden_size):
        super().__init__()
        self.net1 = MLP(input_size, 1, hidden_size)
        self.net2 = MLP(input_size, 1, hidden_size)

    def forward(self, state, action):
        state_action = torch.cat([state, action], 1)
        return self.net1(state_action), self.net2(state_action)

class ReplayBuffer():

    def __init__(self, length):

        self.buffer = deque(maxlen=length)

    def loader(self, batch_size, gradient_steps):

        size = batch_size * gradient_steps
        seed = random.randint(0, 100000)
        sets = [random.Random(seed).choices(x, k=size) for x in zip(*self.buffer)]

        loaders = [DataLoader(s, batch_size) for s in sets]

        return loaders

    def sample(self, amount):

        return random.sample(self.buffer, amount)

    def push(self, state):
        im, control_history = state[0]
        action = state[1]
        reward = state[2]
        next_im, next_history = state[3]
        not_done = state[4]

        self.buffer.append([torch.FloatTensor(x).to(device) for x in [im, control_history, action, reward, next_im, next_history, not_done]])


class AE_SAC:

    def __init__(self, parameters={}):

        params = {
            "gamma": 0.99,
            "tau": 0.005,
            "lr": 0.0001,
            "replay_buffer_size": 1000000,
            "hidden_size": 100,
            "batch_size": 64,
            "n_episodes": 1000,
            "n_random_episodes": 10,
            "discount": 0.9,
            "horizon": 50,
            "im_rows": 40,
            "im_cols": 40,
            "linear_output": 64,
            "target_entropy": -2
        }

        for arg in parameters["sac"].keys():
            params[arg] = parameters["sac"][arg]


        self.gamma = params["gamma"]
        self.tau = params["tau"]
        self.lr = params["lr"]
        self.replay_buffer_size = params["replay_buffer_size"]
        self.hidden_size = params["hidden_size"]
        self.batch_size = params["batch_size"]
        self.n_episodes = params["n_episodes"]
        self.n_random_episodes = params["n_random_episodes"]
        self.discount = params["discount"]
        self.horizon = params["horizon"]
        self.im_rows = params["im_rows"]
        self.im_cols = params["im_cols"]
        self.linear_output = params["linear_output"]
        self.target_entropy = params["target_entropy"]
        self.encoder_critic_loss =  params["encoder_critic_loss"]
        self.encoder_ae_loss = params["encoder_ae_loss"]
        self.act_size = 2

        self.encoder_update_frequency = params["encoder_update_frequency"]


        self.critic = Critic(self.linear_output + self.act_size, self.hidden_size).to(device)

        # Select which parameters to contain in the critic optimizer

        if params["pretrained_ae"]:
            if os.path.isfile(params["pretrained_ae"]):
                self.encoder = torch.load(params["pretrained_ae"])
            else:
                self.encoder = AE(parameters["ae"])
                self.pretrain_ae(params["image_folder"], params["n_images"], params["im_size"], params["pretrained_ae"], params["epochs"])

            self.encoder_update_frequency = 0    
            critic_parameters = list(self.critic.parameters())
        else:
            self.encoder = AE(parameters["ae"])
            
        if self.encoder_critic_loss:
            critic_parameters = list(self.critic.parameters()) + self.encoder.parameters
        else:
            critic_parameters = list(self.critic.parameters())
        
        self.critic_optimizer = torch.optim.Adam(critic_parameters, lr=self.lr)

        self.critic_target = Critic(self.linear_output + self.act_size, self.hidden_size).to(device)
        for target_param, param in zip(self.critic_target.parameters(), self.critic.parameters()):
            target_param.data.copy_(param.data)

        self.actor = Actor(self.linear_output, self.act_size, self.hidden_size).to(device)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=self.lr)

        
        self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
        self.alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=self.lr)

        self.replay_buffer = ReplayBuffer(length=self.replay_buffer_size)


    def update_parameters(self, gradient_steps):
        
        print("Buffer length: {}".format(len(self.replay_buffer.buffer)))

        training_start = time.time_ns()

        # Form dataset from the replay buffer
        loaders = self.replay_buffer.loader(self.batch_size, gradient_steps)
        iters = [iter(l) for l in loaders]

        epoch_encoder_loss = 0
        epoch_critic_loss = 0
        epoch_actor_loss = 0
        e_loss = 0

        for i in range(len(loaders[0])):

            step_start = time.time_ns()

            im, control, action, reward, next_im, next_control, not_done = [next(it) for it in iters]

            # Embedd  images

            embedding, log_sigma = self.encoder.encoder(im)

            with torch.no_grad():
                next_embedding, _ = self.encoder.encoder_target(next_im)

            # Form state vectors

            state = torch.cat([embedding, control], axis=1)
            next_state = torch.cat([next_embedding, next_control], axis=1)

            alpha = self.log_alpha.exp().item()

            # Calculate critic loss

            with torch.no_grad():
                next_action, next_action_log_prob = self.actor.sample(next_state)
                q1_next, q2_next = self.critic_target(next_state, next_action)
                q_next = torch.min(q1_next, q2_next)
                value_next = q_next - alpha * next_action_log_prob
                q_target = reward + not_done * self.gamma * value_next

            q1, q2 = self.critic(state, action)
            q1_loss = 0.5*F.mse_loss(q1, q_target)
            q2_loss = 0.5*F.mse_loss(q2, q_target)
            critic_loss = q1_loss + q2_loss

            loss = critic_loss

            # Calculate encoder loss

            if self.encoder_critic_loss and self.encoder_ae_loss:

                encoder_loss = self.encoder.loss(im, (embedding, log_sigma))
                loss += encoder_loss
                e_loss = encoder_loss.item()
                self.encoder.update_encoder_target()

            # Update critic (and encoder)

            self.critic_optimizer.zero_grad()       
            loss.backward()
            self.critic_optimizer.step()


            if self.encoder_ae_loss and (i % self.encoder_update_frequency) == 0 and not self.encoder_critic_loss:
                
                #Update encoder if only VAE loss is used

                encoder_loss = self.encoder.loss(im)
                self.encoder.optimizer.zero_grad()
                encoder_loss.backward()
                self.encoder.optimizer.step()

                self.encoder.update_encoder_target()

                e_loss = encoder_loss.item()

            for target_param, param in zip(self.critic_target.parameters(), self.critic.parameters()):
                target_param.data.copy_((1.0-self.tau)*target_param.data + self.tau*param.data)

            # Update actor

            state = state.detach()

            action_new, action_new_log_prob = self.actor.sample(state)
            q1_new, q2_new = self.critic(state, action_new)
            q_new = torch.min(q1_new, q2_new)
            actor_loss = (alpha*action_new_log_prob - q_new).mean()

            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            # Update alpha

            alpha_loss = -(self.log_alpha * (action_new_log_prob + self.target_entropy).detach()).mean()

            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

            # Print analytics

            step_time = (time.time_ns() - step_start) / 1e6
            total_time = (time.time_ns() - training_start) / 1e9

            epoch_encoder_loss += e_loss
            epoch_critic_loss += critic_loss.item()
            epoch_actor_loss += actor_loss.item()

            epoch_size = self.batch_size * (i + 1)

            if i % 50 == 0:
                print("Step: {}, Step time: {:.2f}, Total time: {:.2f}, Critic loss: {:.2f}, Encoder loss: {:.2f}, Actor loss: {:.2f}, Alpha: {:.2f}"
                  .format(i, step_time, total_time, epoch_critic_loss / epoch_size, epoch_encoder_loss / epoch_size, epoch_actor_loss / epoch_size, alpha))


    def select_action(self, state):

        embedding = self.encoder.embed(state[0])
        action = torch.FloatTensor(state[1].reshape(1, -1)).to(device)
        state_action = torch.cat([embedding, action], axis=1)

        return self.actor.select_action(state_action)

    def push_buffer(self, state):
        self.replay_buffer.push(state)

    def process_im(self, im, im_size, rgb):

        """ Preprocess image for the agent. """

        im = im[40:,:]
        im = im / 255
        im = cv2.resize(im, (im_size, im_size))

        if rgb:
            im = np.rollaxis(im, 2, 0)
        else:
            im = np.dot(im, [0.299, 0.587, 0.114])[np.newaxis, ...]

        return im

    def export_parameters(self):
        params = {
            "encoder": self.encoder.encoder.cpu().state_dict(),
            "policy": self.actor.cpu().state_dict()
        }
        self.encoder.encoder.to(device)
        self.actor.to(device)

        return params

    def import_parameters(self, params):

        self.encoder.encoder.load_state_dict(params["encoder"])
        self.actor.load_state_dict(params["policy"])

    def append_buffer(self, new_observations):
        for state in new_observations:
            self.push_buffer(state)


            mirror_image = np.flip(state[0][0], 2).copy()
            mirror_control = np.array([-1, 1, 1] * int(len(state[0][1]) / 3)) * np.array(state[0][1])
            mirror_action = [state[1][0] * -1, state[1][1]]

            mirror_next_image = np.flip(state[3][0], 2).copy()
            mirror_next_control = np.array([-1, 1, 1] * int(len(state[0][1]) / 3)) * np.array(state[3][1])

            #print(state[0][0].shape)
            #print(mirror_image.shape)
            #self.push_buffer([[mirror_image, mirror_control], mirror_action, state[2], [mirror_next_image, mirror_next_control], state[4]])

    def pretrain_ae(self, image_folder, n_images, im_size, model_file, epochs):

        """
        Function for pretraining AE in the beginning of the episode using previously recorded images.
        """

        files = [image_folder + x for x in os.listdir(image_folder) if "cam" in x]
        files = random.sample(files, min(len(files), n_images))
        images = []

        print("Loading {} images".format(len(files)))
        ims = len(files)
        for i, file in enumerate(files):
            os.system('clear')
            print("Loading image {}/{}".format(i, ims))
            im = plt.imread(file, format="jpeg")

            images.append(torch.FloatTensor(self.process_im(im, im_size, 0)).to(device))

        loader = DataLoader(images, shuffle=True, batch_size=128)

        try:

            for e in range(epochs):
                epoch_loss = 0
                for i, ims in enumerate(loader):
                
                    encoder_loss = self.encoder.loss(ims)
                    self.encoder.optimizer.zero_grad()
                    encoder_loss.backward()
                    self.encoder.optimizer.step()

                    self.encoder.update_encoder_target()

                    epoch_loss += encoder_loss.item()

                print("Epoch: {}, Encoder loss: {}".format(e + 1, epoch_loss / len(images)))

        except KeyboardInterrupt:
            pass        


        torch.save(self.encoder, model_file)
