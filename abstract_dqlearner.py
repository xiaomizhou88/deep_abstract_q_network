import random
import configparser

import interfaces
import tensorflow as tf
import numpy as np
import tf_helpers as th


class DQLearner:

    def __init__(self, config_file, restore_network_file=None):
        # Set configuration params
        config = configparser.ConfigParser()
        config.read(config_file)
        self.frame_history = config['FRAME_HISTORY']
        self.replay_start_size = config['REPLAY_START_SIZE']
        self.epsilon_start = config['EPSILON_START']
        self.epsilon = self.epsilon_start
        self.epsilon = dict()
        self.epsilon_min = config['EPSILON_END']
        self.epsilon_steps = config['EPSILON_STEPS']
        self.epsilon_delta = (self.epsilon_start - self.epsilon_min) / self.epsilon_steps
        self.update_freq = config['NETWORK_UPDATE_FREQ']
        self.target_copy_freq = config['TARGET_COPY_FREQ']
        self.action_ticker = 1
        self.num_actions = config['NUM_ACTIONS']
        self.batch_size = config['BATCH_SIZE']
        self.max_mmc_path_length = config['MAX_MMC_PATH_LENGTH']
        self.mmc_beta = config['MMC_BETA']
        self.gamma = config['GAMMA']
        self.double = config['DOUBLE']
        self.use_mmc = config['USE_MMC']
        error_clip = config['ERROR_CLIP']
        learning_rate = config['LEARNING_RATE']

        # Set tensorflow config
        tf_config = tf.ConfigProto()
        tf_config.gpu_options.allow_growth = True
        tf_config.allow_soft_placement = True
        self.sess = tf.Session(config=tf_config)

        # Setup tensorflow placeholders
        self.inp_actions = tf.placeholder(tf.float32, [None, self.num_actions])
        inp_shape = [None, 84, 84, self.frame_history]
        inp_dtype = 'uint8'
        assert type(inp_dtype) is str
        self.inp_frames = tf.placeholder(inp_dtype, inp_shape)
        self.inp_sp_frames = tf.placeholder(inp_dtype, inp_shape)
        self.inp_terminated = tf.placeholder(tf.bool, [None])
        self.inp_reward = tf.placeholder(tf.float32, [None])
        self.inp_mmc_reward = tf.placeholder(tf.float32, [None])
        self.inp_mask = tf.placeholder(inp_dtype, [None, self.frame_history])
        self.inp_sp_mask = tf.placeholder(inp_dtype, [None, self.frame_history])

        # Setup Q-Networks
        with tf.variable_scope('online'):
            mask_shape = [-1, 1, 1, self.frame_history]
            mask = tf.reshape(self.inp_mask, mask_shape)
            masked_input = self.inp_frames * mask
            self.q_online = self.construct_q_network(masked_input)
        with tf.variable_scope('target'):
            mask_shape = [-1, 1, 1, self.frame_history]
            sp_mask = tf.reshape(self.inp_sp_mask, mask_shape)
            masked_sp_input = self.inp_sp_frames * sp_mask
            self.q_target = self.construct_q_network(masked_sp_input)
        if self.double:
            with tf.variable_scope('online', reuse=True):
                self.q_online_prime = self.construct_q_network(masked_sp_input)
                print self.q_online_prime
            self.maxQ = tf.gather_nd(self.q_target, tf.transpose(
                [tf.range(0, self.batch_size, dtype=tf.int32),
                 tf.cast(tf.argmax(self.q_online_prime, axis=1), tf.int32)],
                [1, 0]))
        else:
            self.maxQ = tf.reduce_max(self.q_target, axis=1)

        # Create loss handle
        self.r = tf.sign(self.inp_reward)
        use_backup = tf.cast(tf.logical_not(self.inp_terminated), dtype=tf.float32)
        self.y = self.r + use_backup * self.gamma * self.maxQ
        self.delta_dqn = tf.reduce_sum(self.inp_actions * self.q_online, axis=1) - self.y
        self.error_dqn = tf.where(tf.abs(self.delta_dqn) < error_clip, 0.5 * tf.square(self.delta_dqn),
                                  error_clip * tf.abs(self.delta_dqn))
        if self.use_mmc:
            self.delta_mmc = tf.reduce_sum(self.inp_actions * self.q_online, axis=1) - self.inp_mmc_reward
            self.error_mmc = tf.where(tf.abs(self.delta_mmc) < error_clip, 0.5 * tf.square(self.delta_mmc),
                                      error_clip * tf.abs(self.delta_mmc))
            self.loss = (1. - self.mmc_beta) * tf.reduce_sum(self.error_dqn) + self.mmc_beta * tf.reduce_sum(
                self.error_mmc)
        else:
            self.loss = tf.reduce_sum(self.error_dqn)

        # Create optimizer
        optimizer = tf.train.RMSPropOptimizer(learning_rate=learning_rate, decay=0.95, centered=True, epsilon=0.01)
        self.train_op = optimizer.minimize(self.loss, var_list=th.get_vars('online'))
        self.copy_op = th.make_copy_op('online', 'target')
        self.saver = tf.train.Saver(var_list=th.get_vars('online'))
        self.sess.run(tf.initialize_all_variables())

        # Optionally load previous weights
        if restore_network_file is not None:
            self.saver.restore(self.sess, restore_network_file)
            print 'Restored network from file'
        self.sess.run(self.copy_op)

    def save_network(self, file_name):
        self.saver.save(self.sess, file_name)

    def run_learning_episode(self, environment, episode_dict, max_episode_steps=100000):
        episode_steps = 0
        total_reward = 0
        episode_finished = False

        for step in range(max_episode_steps):
            if environment.is_current_state_terminal() or self.extra_termination_conditions(step, episode_dict):
                break

            state = environment.get_current_state()
            if np.random.uniform(0, 1) < self.epsilon:
                action = np.random.choice(environment.get_actions_for_state(state))
                # action = self.get_safe_explore_action(state, environment)
            else:
                action = self.get_action(state, episode_dict)

            if self.action_ticker > self.replay_start_size:
                self.epsilon = max(self.epsilon_min, self.epsilon - self.epsilon_delta)

            state, action, env_reward, next_state, is_terminal = environment.perform_action(action)
            total_reward += env_reward

            self.add_experience_to_replay(state, action, env_reward, next_state, is_terminal, episode_dict)

            if (self.action_ticker > self.replay_start_size) and (self.action_ticker % self.update_freq == 0):
                loss = self.update_q_values()
            if (self.action_ticker - self.replay_start_size) % self.target_copy_freq == 0:
                self.sess.run(self.copy_op)

            self.action_ticker += 1
            episode_steps += 1

            if episode_finished:
                break

        return episode_steps, total_reward

    def construct_q_network(self, network_input):
        raise NotImplemented

    def update_q_values(self):
        raise NotImplemented

    def extra_termination_conditions(self, step, episode_dict):
        return False

    def get_action(self, state, episode_dict):
        raise NotImplemented

    def add_experience_to_replay(self, state, action, env_reward, next_state, is_terminal, episode_dict):
        raise NotImplemented
