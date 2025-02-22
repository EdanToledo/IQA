import random
import yaml
import copy
from collections import namedtuple
from os.path import join as pjoin

import spacy
import numpy as np
import torch
import torch.nn.functional as F
from torch.distributions import Categorical
from torch.nn.utils.rnn import pad_sequence

import command_generation_memory
import qa_memory
from model import ActorCritic, DQN, ICM
from layers import compute_mask, NegativeLogLoss
from generic import to_np, to_pt, preproc, _words_to_ids, pad_sequences
from generic import max_len, ez_gather_dim_1, ObservationPool
from generic import list_of_token_list_to_char_input


class Agent:
    def __init__(self, config_file_path):

        self.mode = "train"
        with open(config_file_path) as reader:
            self.config = yaml.safe_load(reader)
        print(self.config)
        self.load_config()

        if not self.a2c:
            # Create DQN
            self.online_net = DQN(
                config=self.config,
                word_vocab=self.word_vocab,
                char_vocab=self.char_vocab,
                answer_type=self.answer_type,
            )
            self.target_net = DQN(
                config=self.config,
                word_vocab=self.word_vocab,
                char_vocab=self.char_vocab,
                answer_type=self.answer_type,
            )
            self.online_net.train()
            self.target_net.train()
            self.update_target_net()
            for param in self.target_net.parameters():
                param.requires_grad = False

            if self.use_cuda:
                self.online_net.cuda()
                self.target_net.cuda()
        else:
            # Create the Policy agent
            self.online_net = ActorCritic(
                config=self.config,
                word_vocab=self.word_vocab,
                char_vocab=self.char_vocab,
                answer_type=self.answer_type,
            )
            self.online_net.train()
            if self.use_cuda:
                self.online_net.cuda()

        self.naozi = ObservationPool(capacity=self.naozi_capacity)
        # optimizer

        self.optimizer = torch.optim.Adam(
            self.online_net.parameters(),
            lr=self.config["training"]["optimizer"]["learning_rate"],
        )

        self.clip_grad_norm = self.config["training"]["optimizer"]["clip_grad_norm"]

    def load_config(self):
        """
        Load the config file and set all agent parameters accordingly.
        """
        # word vocab
        with open("vocabularies/word_vocab.txt") as f:
            self.word_vocab = f.read().split("\n")
        self.word2id = {}
        for i, w in enumerate(self.word_vocab):
            self.word2id[w] = i
        # char vocab
        with open("vocabularies/char_vocab.txt") as f:
            self.char_vocab = f.read().split("\n")
        self.char2id = {}
        for i, w in enumerate(self.char_vocab):
            self.char2id[w] = i

        self.EOS_id = self.word2id["</s>"]
        self.train_data_size = self.config["general"]["train_data_size"]
        self.question_type = self.config["general"]["question_type"]
        self.random_map = self.config["general"]["random_map"]
        self.testset_path = self.config["general"]["testset_path"]
        self.naozi_capacity = self.config["general"]["naozi_capacity"]
        self.eval_folder = pjoin(
            self.testset_path,
            self.question_type,
            ("random_map" if self.random_map else "fixed_map"),
        )
        self.eval_data_path = pjoin(self.testset_path, "data.json")

        self.batch_size = self.config["training"]["batch_size"]
        self.max_nb_steps_per_episode = self.config["training"][
            "max_nb_steps_per_episode"
        ]
        self.max_episode = self.config["training"]["max_episode"]
        self.target_net_update_frequency = self.config["training"][
            "target_net_update_frequency"
        ]
        self.learn_start_from_this_episode = self.config["training"][
            "learn_start_from_this_episode"
        ]

        self.run_eval = self.config["evaluate"]["run_eval"]
        self.eval_batch_size = self.config["evaluate"]["batch_size"]
        self.eval_max_nb_steps_per_episode = self.config["evaluate"][
            "max_nb_steps_per_episode"
        ]

        # dueling networks
        self.dueling_networks = self.config["dueling_networks"]

        # double dqn
        self.double_dqn = self.config["double_dqn"]

        # A2C
        self.a2c = self.config["a2c"]["enable"]
        self.entropy_coeff = self.config["a2c"]["entropy_coefficient"]

        # ICM
        self.icm = self.config["icm"]["enable"]
        self.use_intrinsic_reward = self.config["icm"]["use_intrinsic_reward"]

        # Freeze the encoding of state - i.e don't update transformers weights - only tune the dqn or policy
        self.freeze_encoding = self.config["model"]["freeze_encoding"]

        # Set the random seed manually for reproducibility.
        self.random_seed = self.config["general"]["random_seed"]
        np.random.seed(self.random_seed)
        torch.manual_seed(self.random_seed)
        if torch.cuda.is_available():
            if not self.config["general"]["use_cuda"]:
                print(
                    "WARNING: CUDA device detected but 'use_cuda: false' found in config.yaml"
                )
                self.use_cuda = False
                self.device = torch.device("cpu")
            else:
                torch.backends.cudnn.deterministic = True
                torch.cuda.manual_seed(self.random_seed)
                self.use_cuda = True

                self.device = torch.device("cuda")
        else:
            self.use_cuda = False
            self.device = torch.device("cpu")

        if self.question_type == "location":
            self.answer_type = "pointing"
        elif self.question_type in ["attribute", "existence"]:
            self.answer_type = "2 way"
        else:
            raise NotImplementedError

        self.save_checkpoint = self.config["checkpoint"]["save_checkpoint"]
        self.experiment_tag = self.config["checkpoint"]["experiment_tag"]
        self.save_frequency = self.config["checkpoint"]["save_frequency"]
        self.load_pretrained = self.config["checkpoint"]["load_pretrained"]
        self.load_from_tag = self.config["checkpoint"]["load_from_tag"]

        self.qa_loss_lambda = self.config["training"]["qa_loss_lambda"]
        self.interaction_loss_lambda = self.config["training"][
            "interaction_loss_lambda"
        ]

        # replay buffer and updates
        self.discount_gamma = self.config["replay"]["discount_gamma"]
        self.replay_batch_size = self.config["replay"]["replay_batch_size"]
        if self.a2c:
            self.command_generation_replay_memory = (
                command_generation_memory.SingleEpisodeStorage()
            )
        else:
            self.command_generation_replay_memory = (
                command_generation_memory.PrioritizedReplayMemory(
                    self.config["replay"]["replay_memory_capacity"],
                    priority_fraction=self.config["replay"][
                        "replay_memory_priority_fraction"
                    ],
                    discount_gamma=self.discount_gamma,
                )
            )
        self.qa_replay_memory = qa_memory.PrioritizedReplayMemory(
            self.config["replay"]["replay_memory_capacity"], priority_fraction=0.0
        )
        self.update_per_k_game_steps = self.config["replay"]["update_per_k_game_steps"]
        self.multi_step = self.config["replay"]["multi_step"]

        # distributional RL
        self.use_distributional = self.config["distributional"]["enable"]
        self.atoms = self.config["distributional"]["atoms"]
        self.v_min = self.config["distributional"]["v_min"]
        self.v_max = self.config["distributional"]["v_max"]
        self.support = torch.linspace(
            self.v_min, self.v_max, self.atoms, device=self.device
        )  # Support (range) of z

        self.delta_z = (self.v_max - self.v_min) / (self.atoms - 1)

        # counting reward
        self.revisit_counting_lambda_anneal_episodes = self.config[
            "episodic_counting_bonus"
        ]["revisit_counting_lambda_anneal_episodes"]
        self.revisit_counting_lambda_anneal_from = self.config[
            "episodic_counting_bonus"
        ]["revisit_counting_lambda_anneal_from"]
        self.revisit_counting_lambda_anneal_to = self.config["episodic_counting_bonus"][
            "revisit_counting_lambda_anneal_to"
        ]
        self.revisit_counting_lambda = self.revisit_counting_lambda_anneal_from

        # valid command bonus
        self.valid_command_bonus_lambda = self.config["valid_command_bonus_lambda"]

        # epsilon greedy
        self.epsilon_anneal_episodes = self.config["epsilon_greedy"][
            "epsilon_anneal_episodes"
        ]
        self.epsilon_anneal_from = self.config["epsilon_greedy"]["epsilon_anneal_from"]
        self.epsilon_anneal_to = self.config["epsilon_greedy"]["epsilon_anneal_to"]
        self.epsilon = self.epsilon_anneal_from
        self.noisy_net = self.config["epsilon_greedy"]["noisy_net"]
        if self.noisy_net:
            # disable epsilon greedy
            self.epsilon_anneal_episodes = -1
            self.epsilon = 0.0

        self.nlp = spacy.load("en_core_web_sm")
        self.single_word_verbs = set(["inventory", "look", "wait"])
        self.two_word_verbs = set(["go"])

    def train(self):
        """
        Tell the agent that it's in training mode.
        """
        self.mode = "train"
        self.online_net.train()

    def eval(self):
        """
        Tell the agent that it's in evaluation mode.
        """
        self.mode = "eval"
        self.online_net.eval()

    def update_target_net(self):
        """
        Update the target DQN - used for stability in training.
        """
        self.target_net.load_state_dict(self.online_net.state_dict())

    def reset_noise(self):
        """
        Reset the noisy network noise.
        """
        if self.noisy_net:
            # Resets noisy weights in all linear layers (of online net only)
            self.online_net.reset_noise()

    def zero_noise(self):
        """
        Zero the noisy network noise.
        """
        if self.noisy_net:
            self.online_net.zero_noise()
            self.target_net.zero_noise()

    def load_pretrained_model(self, load_from):
        """
        Load pretrained checkpoint from file.

        :param load_from: File name of the pretrained model checkpoint.
        """
        print("loading model from %s\n" % (load_from))
        try:
            
            if self.use_cuda:
                checkpoint = torch.load(load_from)
            else:
                checkpoint = torch.load(load_from, map_location="cpu")
            
            model_state_dict = checkpoint['model_state_dict']
            optim_state_dict = checkpoint['optimizer_state_dict']
            self.online_net.load_state_dict(model_state_dict)
            self.optimizer.load_state_dict(optim_state_dict)
        except:
            print("Failed to load checkpoint...")


    def save_model_to_path(self, save_to):
        """
        Save the pytorch agent model.

        :param save_to: file to save model to.
        """
        torch.save({
            'model_state_dict': self.online_net.state_dict().state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            }, save_to)
        print("Saved checkpoint to %s..." % (save_to))

    def init(self, obs, infos):
        """
        Prepare the agent for the upcoming games by resetting the: unique state counter, the previous actions, both the "not finished" variables, and the observation history.

        :param obs: Previous command's feedback for each game.
        :param infos: Additional information for each game.
        """
        # reset agent, get vocabulary masks for verbs / adjectives / nouns
        batch_size = len(obs)
        self.reset_binarized_counter(batch_size)
        self.not_finished_yet = np.ones((batch_size,), dtype="float32")
        self.prev_actions = [["" for _ in range(batch_size)]]
        # 1s and starts to be 0 when previous action is "wait"
        self.prev_step_is_still_interacting = np.ones((batch_size,), dtype="float32")
        self.naozi.reset(batch_size=batch_size)

    # TLDEDA001
    def pad_commands(self, commands):
        """
        Pad commands with words not IDs to be of size three

        :param commands: list of commands

        :return: list of padded_commands.
        """
        padded_commands = []
        for command in commands:
            padded_command = command
            length = len(command.split())
            if length < 2:
                padded_command += " </s> <pad>"
            elif length < 3:
                padded_command += " </s>"
            padded_commands.append(padded_command)
        return padded_commands

    def get_agent_inputs(self, string_list):
        """
        process agent input strings into their word ids and char ids and convert to pytorch tensor.

        :param string_list: list of string observations for each game in batch.

        :return input_sentence: pytorch tensor of word id sentences padded for entire batch
        :return input_sentence_char: pytorch tensor of char id sentences padded for entire batch
        :return sentence_id_list: 2d list of word ids for each sentence.
        """
        sentence_token_list = [item.split() for item in string_list]
        sentence_id_list = [
            _words_to_ids(tokens, self.word2id) for tokens in sentence_token_list
        ]
        input_sentence_char = list_of_token_list_to_char_input(
            sentence_token_list, self.char2id
        )

        padding_length = max_len(sentence_id_list)

        input_sentence = pad_sequences(sentence_id_list, maxlen=padding_length).astype(
            "int32"
        )
        input_sentence = to_pt(input_sentence, self.use_cuda)
        input_sentence_char = to_pt(input_sentence_char, self.use_cuda)
        return input_sentence, input_sentence_char, sentence_id_list

    def get_game_info_at_certain_step(self, obs, infos):
        """
        Get the processed observation string and possible words to use.

        :param obs: Previous command's feedback for each game.
        :param infos: Additional information for each game.

        :return observation_strings: processed observation string
        :return [possible_verbs, possible_adjs, possible_nouns]: possible words the agent can use
        """
        batch_size = len(obs)
        # The observation strings for each game in the batch processed to be normalised
        feedback_strings = [preproc(item, tokenizer=self.nlp) for item in obs]
        # The description strings for each game in the batch processed to be normalised - i.e output of look command - description of current room
        description_strings = [
            preproc(item, tokenizer=self.nlp) for item in infos["description"]
        ]
        # Process the two strings together for the agent to use
        observation_strings = [
            d + " <|> " + fb if fb != d else d + " <|> hello"
            for fb, d in zip(feedback_strings, description_strings)
        ]
        # get objects in agent inventory
        inventory_strings = [
            preproc(item, tokenizer=self.nlp) for item in infos["inventory"]
        ]
        # Get words that make sense in context
        local_word_list = [
            obs.split() + inv.split()
            for obs, inv in zip(observation_strings, inventory_strings)
        ]

        directions = ["east", "west", "north", "south"]
        if self.question_type in ["location", "existence"]:
            # agents observes the env, but do not change them
            possible_verbs = [
                ["go", "inventory", "wait", "open", "examine"]
                for _ in range(batch_size)
            ]
        else:
            possible_verbs = [
                list(set(item) - set(["", "look"])) for item in infos["verbs"]
            ]

        possible_adjs, possible_nouns = [], []
        for i in range(batch_size):
            object_nouns = [item.split()[-1] for item in infos["object_nouns"][i]]
            object_adjs = [w for item in infos["object_adjs"][i] for w in item.split()]
            possible_nouns.append(
                list(set(object_nouns) & set(local_word_list[i]) - set([""]))
                + directions
            )
            possible_adjs.append(
                list(set(object_adjs) & set(local_word_list[i]) - set([""])) + ["</s>"]
            )

        return observation_strings, [possible_verbs, possible_adjs, possible_nouns]

    def get_state_strings(self, infos):
        """
        Get strings about environment:

        1. the description of the current room the agent is in.
        2. what is in the agents inventory.

        Process these strings together and return.

        :param infos: the game environment infos object.

        :return observation_strings: the strings concatenated together for each game in the batch
        """
        description_strings = infos["description"]
        inventory_strings = infos["inventory"]
        observation_strings = [
            _d + _i for (_d, _i) in zip(description_strings, inventory_strings)
        ]

        return observation_strings

    def get_local_word_masks(self, possible_words):
        """
        Get masks for vocab of possible verbs, noun, adjectives
        i.e an array of size vocab that contains zeroes except
        in the indexes of the possible words where the array contains a one.

        :param possible_words: array of three items -  possible_verbs, possible_adjs, possible_nouns

        :return [verb_mask, adj_mask, noun_mask]: masks of each of the word lists
        """
        possible_verbs, possible_adjs, possible_nouns = possible_words
        batch_size = len(possible_verbs)

        verb_mask = np.zeros((batch_size, len(self.word_vocab)), dtype="float32")
        noun_mask = np.zeros((batch_size, len(self.word_vocab)), dtype="float32")
        adj_mask = np.zeros((batch_size, len(self.word_vocab)), dtype="float32")
        for i in range(batch_size):
            for w in possible_verbs[i]:
                if w in self.word2id:
                    verb_mask[i][self.word2id[w]] = 1.0
            for w in possible_adjs[i]:
                if w in self.word2id:
                    adj_mask[i][self.word2id[w]] = 1.0
            for w in possible_nouns[i]:
                if w in self.word2id:
                    noun_mask[i][self.word2id[w]] = 1.0
        adj_mask[:, self.EOS_id] = 1.0

        return [verb_mask, adj_mask, noun_mask]

    def get_match_representations(
        self,
        input_observation,
        input_observation_char,
        input_quest,
        input_quest_char,
        use_model="online",
    ):
        """
        This encodes words and chars and aggregates them into the current state representation.

        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param word_masks: the mask to use i.e usually possible words
        :param use_model: which model to use i.e online or target.

        :return match_representation_sequence: the transformed state representation.
        """
        model = self.online_net if use_model == "online" else self.target_net
        (
            description_representation_sequence,
            description_mask,
        ) = model.representation_generator(input_observation, input_observation_char)
        quest_representation_sequence, quest_mask = model.representation_generator(
            input_quest, input_quest_char
        )

        match_representation_sequence = model.get_match_representations(
            description_representation_sequence,
            description_mask,
            quest_representation_sequence,
            quest_mask,
        )
        match_representation_sequence = (
            match_representation_sequence * description_mask.unsqueeze(-1)
        )

        return match_representation_sequence

    def get_ranks(
        self,
        input_observation,
        input_observation_char,
        input_quest,
        input_quest_char,
        word_masks,
        use_model="online",
    ):
        """
        Given input observation and question tensors, to get Q values of words or if using actor critic, the probability dists and state value.

        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param word_masks: the mask to use i.e usually possible words
        :param use_model: which model to use i.e online or target.

        :return action_ranks: either q values or policy vocab distributions.
        """
        model = self.online_net if use_model == "online" else self.target_net

        if self.freeze_encoding:
            with torch.no_grad():
                match_representation_sequence = self.get_match_representations(
                    input_observation,
                    input_observation_char,
                    input_quest,
                    input_quest_char,
                    use_model=use_model,
                )
        else:
            match_representation_sequence = self.get_match_representations(
                input_observation,
                input_observation_char,
                input_quest,
                input_quest_char,
                use_model=use_model,
            )

        # list of 3 tensors size of vocab
        action_ranks = model.action_scorer(match_representation_sequence, word_masks)
        return action_ranks

    def choose_probability_command(self, action_ranks, word_mask=None):
        """
        Generate a command by sampling from action probability distributions -- TLDEDA001

        :param action_ranks: probability distributions of words in command triple.

        :return: action_indices - word ids for selected actions. action_log_probs- log probability of selected actions. action_entropies - entropies of probability distributions
        """

        action_indices = []
        action_log_probs = []
        action_entropies = []

        for i in range(len(action_ranks)):
            batch_log_probs = []
            ar = action_ranks[i]
            batch_indices_dist = torch.distributions.Categorical(ar)
            batch_indices = batch_indices_dist.sample()

            batch_log_probs = batch_indices_dist.log_prob(batch_indices)
            batch_entropy = batch_indices_dist.entropy()

            action_indices.append(batch_indices)
            action_log_probs.append(batch_log_probs)
            action_entropies.append(batch_entropy)

        return action_indices, action_log_probs, action_entropies

    def choose_maxQ_command(self, action_ranks, word_mask=None):
        """
        Generate a command by maximum q values, for epsilon greedy.

        :param action_ranks: the q values generated from the DQN.
        :param word_mask: the word mask to use when choosing max values.

        :return action_indices: return the indices of the selected actions.
        """
        if self.use_distributional:
            action_ranks = [
                (item * self.support).sum(2) for item in action_ranks
            ]  # list of batch x n_vocab
        action_indices = []
        for i in range(len(action_ranks)):
            ar = action_ranks[i]
            # minus the min value, so that all values are non-negative
            ar = ar - torch.min(ar, -1, keepdim=True)[0] + 1e-2
            if word_mask is not None:
                assert word_mask[i].size() == ar.size(), (
                    word_mask[i].size().shape,
                    ar.size(),
                )
                ar = ar * word_mask[i]

            action_indices.append(torch.argmax(ar, -1))  # batch
        return action_indices

    def choose_random_command(self, batch_size, action_space_size, possible_words=None):
        """
        Generate a command randomly, for epsilon greedy.

        :param batch_size: the batch size.
        :param action_space_size: the size of the vocab.
        :param possible_words: possible words that an agent can use.

        :return action_indices: the indices of the random actions.
        """
        action_indices = []
        for i in range(3):
            if possible_words is None:
                indices = np.random.choice(action_space_size, batch_size)
            else:
                indices = []
                for j in range(batch_size):
                    mask_ids = []
                    for w in possible_words[i][j]:
                        if w in self.word2id:
                            mask_ids.append(self.word2id[w])
                    indices.append(np.random.choice(mask_ids))
                indices = np.array(indices)
            action_indices.append(to_pt(indices, self.use_cuda))  # batch

        return action_indices

    def get_chosen_strings(self, chosen_indices):
        """
        Turns list of word indices into actual command strings.

        :param chosen_indices: Word indices chosen by model.

        :return res_str: actual text command
        """
        chosen_indices_np = [to_np(item) for item in chosen_indices]
        res_str = []
        batch_size = chosen_indices_np[0].shape[0]
        for i in range(batch_size):
            verb, adj, noun = (
                chosen_indices_np[0][i],
                chosen_indices_np[1][i],
                chosen_indices_np[2][i],
            )

            res_str.append(self.word_ids_to_commands(verb, adj, noun))
        return res_str

    def word_ids_to_commands(self, verb, adj, noun):
        """
        Turn the 3 indices into actual command strings.

        Arguments:
            verb: Index of the guessing verb in vocabulary
            adj: Index of the guessing adjective in vocabulary
            noun: Index of the guessing noun in vocabulary
        """
        # turns 3 indices into actual command strings
        if self.word_vocab[verb] in self.single_word_verbs:
            return self.word_vocab[verb]
        if self.word_vocab[verb] in self.two_word_verbs:
            return " ".join([self.word_vocab[verb], self.word_vocab[noun]])
        if adj == self.EOS_id:
            return " ".join([self.word_vocab[verb], self.word_vocab[noun]])
        else:
            return " ".join(
                [self.word_vocab[verb], self.word_vocab[adj], self.word_vocab[noun]]
            )

    def act_random(
        self,
        obs,
        infos,
        input_observation,
        input_observation_char,
        input_quest,
        input_quest_char,
        possible_words,
    ):
        """
        choose an action randomly.

        :param obs: list of text observations for each game in batch.
        :param infos: textworld game infos object.
        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param possible_words: the possible words and agent can use based on environment.

        :return chosen_strings: the list of commands for each game in batch.
        :return replay_info: contains the chosen word indices in vocab of commands generated and whether or not agents in the batch are still interacting.
        """
        with torch.no_grad():
            batch_size = len(obs)
            word_indices_random = self.choose_random_command(
                batch_size, len(self.word_vocab), possible_words
            )
            chosen_indices = word_indices_random
            chosen_strings = self.get_chosen_strings(chosen_indices)

            for i in range(batch_size):
                if chosen_strings[i] == "wait":
                    self.not_finished_yet[i] = 0.0

            # info for replay memory
            for i in range(batch_size):
                if self.prev_actions[-1][i] == "wait":
                    self.prev_step_is_still_interacting[i] = 0.0
            # previous step is still interacting, this is because DQN requires one step extra computation
            replay_info = [
                chosen_indices,
                to_pt(self.prev_step_is_still_interacting, self.use_cuda, "float"),
            ]

            # cache new info in current game step into caches
            self.prev_actions.append(chosen_strings)
            return chosen_strings, replay_info

    def act_greedy(
        self,
        obs,
        infos,
        input_observation,
        input_observation_char,
        input_quest,
        input_quest_char,
        possible_words,
    ):
        """
        Acts upon the current list of observations.
        One text command must be returned for each observation.

        :param obs: list of text observations for each game in batch.
        :param infos: textworld game infos object.
        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param possible_words: the possible words and agent can use based on environment.

        :return chosen_strings: the list of commands for each game in batch.
        :return replay_info: contains the chosen word indices in vocab of commands generated and whether or not agents in the batch are still interacting.
        """
        with torch.no_grad():
            batch_size = len(obs)
            local_word_masks_np = self.get_local_word_masks(possible_words)
            local_word_masks = [
                to_pt(item, self.use_cuda, type="float") for item in local_word_masks_np
            ]

            # generate commands for one game step, epsilon greedy is applied, i.e.,
            # there is epsilon of chance to generate random commands
            action_ranks = self.get_ranks(
                input_observation,
                input_observation_char,
                input_quest,
                input_quest_char,
                local_word_masks,
                use_model="online",
            )  # list of batch x vocab
            word_indices_maxq = self.choose_maxQ_command(action_ranks, local_word_masks)
            chosen_indices = word_indices_maxq

            chosen_strings = self.get_chosen_strings(chosen_indices)

            for i in range(batch_size):
                if chosen_strings[i] == "wait":
                    self.not_finished_yet[i] = 0.0

            # info for replay memory
            for i in range(batch_size):
                if self.prev_actions[-1][i] == "wait":
                    self.prev_step_is_still_interacting[i] = 0.0
            # previous step is still interacting, this is because DQN requires one step extra computation
            replay_info = [
                chosen_indices,
                to_pt(self.prev_step_is_still_interacting, self.use_cuda, "float"),
            ]

            # cache new info in current game step into caches
            self.prev_actions.append(chosen_strings)
            return chosen_strings, replay_info

    def act(
        self,
        obs,
        infos,
        input_observation,
        input_observation_char,
        input_quest,
        input_quest_char,
        possible_words,
        random=False,
    ):
        """
        Acts upon the current list of observations.
        One text command must be returned for each observation.

        :param obs: list of text observations for each game in batch.
        :param infos: textworld game infos object.
        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param possible_words: the possible words and agent can use based on environment.
        :param random: boolean to act randomly.

        :return chosen_strings: the list of commands for each game in batch.
        :return replay_info: contains the chosen word indices in vocab of commands generated and whether or not agents in the batch are still interacting.
        """
        # if policy agent: -- TLDEDA001
        if self.a2c:
            batch_size = len(obs)

            local_word_masks_np = self.get_local_word_masks(possible_words)
            local_word_masks = [
                to_pt(item, self.use_cuda, type="float") for item in local_word_masks_np
            ]

            # generate commands for one game step, epsilon greedy is applied, i.e.,
            # there is epsilon of chance to generate random commands

            probs, value = self.get_ranks(
                input_observation,
                input_observation_char,
                input_quest,
                input_quest_char,
                local_word_masks,
                use_model="online",
            )  # list of batch x vocab
            value = value.permute(1, 0).squeeze(0)

            (
                chosen_indices,
                action_log_probs,
                action_entropies,
            ) = self.choose_probability_command(probs)

            chosen_strings = self.get_chosen_strings(chosen_indices)

            for i in range(batch_size):
                if chosen_strings[i] == "wait":
                    self.not_finished_yet[i] = 0.0

            # info for replay memory
            for i in range(batch_size):
                if self.prev_actions[-1][i] == "wait":
                    self.prev_step_is_still_interacting[i] = 0.0

            replay_info = [
                chosen_indices,
                value,
                action_log_probs,
                action_entropies,
                to_pt(self.prev_step_is_still_interacting, self.use_cuda, "float"),
            ]

            # cache new info in current game step into caches
            self.prev_actions.append(chosen_strings)
            return chosen_strings, replay_info

        else:
            with torch.no_grad():
                if self.mode == "eval":
                    return self.act_greedy(
                        obs,
                        infos,
                        input_observation,
                        input_observation_char,
                        input_quest,
                        input_quest_char,
                        possible_words,
                    )
                if random:
                    return self.act_random(
                        obs,
                        infos,
                        input_observation,
                        input_observation_char,
                        input_quest,
                        input_quest_char,
                        possible_words,
                    )
                batch_size = len(obs)

                local_word_masks_np = self.get_local_word_masks(possible_words)
                local_word_masks = [
                    to_pt(item, self.use_cuda, type="float")
                    for item in local_word_masks_np
                ]

                # generate commands for one game step, epsilon greedy is applied, i.e.,
                # there is epsilon of chance to generate random commands
                action_ranks = self.get_ranks(
                    input_observation,
                    input_observation_char,
                    input_quest,
                    input_quest_char,
                    local_word_masks,
                    use_model="online",
                )  # list of batch x vocab

                word_indices_maxq = self.choose_maxQ_command(
                    action_ranks, local_word_masks
                )
                word_indices_random = self.choose_random_command(
                    batch_size, len(self.word_vocab), possible_words
                )

                # random number for epsilon greedy
                rand_num = np.random.uniform(low=0.0, high=1.0, size=(batch_size,))
                less_than_epsilon = (rand_num < self.epsilon).astype("float32")  # batch
                greater_than_epsilon = 1.0 - less_than_epsilon
                less_than_epsilon = to_pt(less_than_epsilon, self.use_cuda, type="long")
                greater_than_epsilon = to_pt(
                    greater_than_epsilon, self.use_cuda, type="long"
                )
                chosen_indices = [
                    less_than_epsilon * idx_random + greater_than_epsilon * idx_maxq
                    for idx_random, idx_maxq in zip(
                        word_indices_random, word_indices_maxq
                    )
                ]

                chosen_strings = self.get_chosen_strings(chosen_indices)

                for i in range(batch_size):
                    if chosen_strings[i] == "wait":
                        self.not_finished_yet[i] = 0.0

                # info for replay memory
                for i in range(batch_size):
                    if self.prev_actions[-1][i] == "wait":
                        self.prev_step_is_still_interacting[i] = 0.0
                # previous step is still interacting, this is because DQN requires one step extra computation

                replay_info = [
                    chosen_indices,
                    to_pt(self.prev_step_is_still_interacting, self.use_cuda, "float"),
                ]

                # cache new info in current game step into caches
                self.prev_actions.append(chosen_strings)
                return chosen_strings, replay_info

    def get_actor_critic_loss(self):
        """
        Calculate policy agent loss -- tldeda001

        :return loss: pytorch loss tensor.
        """

        data = self.command_generation_replay_memory.get_batch()
        if data is None:
            return None

        (
            state_list,
            action_list,
            next_state_list,
            obs_list,
            quest_list,
            possible_words_list,
            word_indices_list,
            rewards,
            state_values,
            action_log_probs,
            action_entropies,
            is_finals,
        ) = data

        next_obs_list = obs_list[1:]
        obs_list = obs_list[0:-1]
        quest_list = quest_list[1:]
        action_list = action_list[0:-1]

        finals_mask = 1 - to_pt(np.array(is_finals, dtype=bool), self.use_cuda)

        # If using ICM - calculate icm loss
        if self.icm:
            input_quest, input_quest_char, _ = self.get_agent_inputs(quest_list)
            input_state, input_state_chars, _ = self.get_agent_inputs(obs_list)
            input_next_state, input_next_state_chars, _ = self.get_agent_inputs(
                next_obs_list
            )
            if self.freeze_encoding:
                with torch.no_grad():
                    encoded_states = self.get_match_representations(
                        input_state, input_state_chars, input_quest, input_quest_char
                    )
                    encoded_next_states = self.get_match_representations(
                        input_next_state,
                        input_next_state_chars,
                        input_quest,
                        input_quest_char,
                    )
            else:
                encoded_states = self.get_match_representations(
                    input_state, input_state_chars, input_quest, input_quest_char
                )
                encoded_next_states = self.get_match_representations(
                    input_next_state,
                    input_next_state_chars,
                    input_quest,
                    input_quest_char,
                )

            action_inputs = torch.stack(action_list)
            encoded_actions, _ = self.online_net.word_embedding(action_inputs)

            if self.online_net.curiosity_module.beta == 0:
                icm_loss = (
                    self.online_net.curiosity_module.get_inverse_loss(
                        encoded_states, action_inputs, encoded_next_states
                    )
                ).mean()
            elif self.online_net.curiosity_module.beta == 1:
                icm_loss = (
                    self.online_net.curiosity_module.get_forward_loss(
                        encoded_states, encoded_actions, encoded_next_states
                    )
                ).mean()
            else:
                inverse_loss = (
                    self.online_net.curiosity_module.get_inverse_loss(
                        encoded_states, action_inputs, encoded_next_states
                    )
                ).mean()
                forward_loss = (
                    self.online_net.curiosity_module.get_forward_loss(
                        encoded_states, encoded_actions, encoded_next_states
                    )
                ).mean()
                icm_loss = (
                    (1 - self.online_net.curiosity_module.beta) * inverse_loss
                    + self.online_net.curiosity_module.beta * forward_loss
                )

        policy_losses = []
        value_losses = []
        returns = []
        Gt = 0.0

        # calculate the true value using rewards returned from the environment
        for i, reward in enumerate(rewards[::-1]):
            if is_finals[::-1][i]:
                Gt = 0.0
            Gt = reward + self.discount_gamma * Gt
            returns.insert(0, Gt)

        returns = torch.stack(returns)

        state_values = torch.stack(state_values)

        advantage = returns - state_values * finals_mask
        concat_probs = []
        concat_entropies = []

        for i, logs in enumerate(action_log_probs):
            concat_entropies.append(torch.stack(action_entropies[i]))
            concat_probs.append(torch.stack(logs))

        # calculate entropy loss
        entropy_loss = torch.stack(concat_entropies).mean()
        # calculate actor (policy) loss
        policy_losses = torch.tensor(0.0, dtype=torch.float16, device=self.device)

        for i in range(3):
            policy_losses += (
                -torch.stack(concat_probs)[:, i] * advantage.detach()
            ).mean()

        value_losses = 0.5 * advantage.pow(2).mean()

        # sum up all the values of policy_losses and value_losses
        actor_critic_loss = (
            policy_losses + value_losses - self.entropy_coeff * entropy_loss
        )
        if self.icm:
            loss = (
                self.online_net.curiosity_module.lambda_weight * actor_critic_loss
                + self.online_net.curiosity_module.loss_weight * icm_loss
            )
        else:
            loss = actor_critic_loss

        return loss

    def get_dqn_loss(self):
        """
        Update neural model in agent. In this example we follow algorithm
        of updating model in dqn with replay memory.

        :return loss: pytorch loss tensor.
        """
        if len(self.command_generation_replay_memory) < self.replay_batch_size:
            return None

        data = self.command_generation_replay_memory.get_batch(
            self.replay_batch_size, self.multi_step
        )
        if data is None:
            return None

        (
            state_list,
            action_list,
            next_state_list,
            obs_list,
            quest_list,
            possible_words_list,
            chosen_indices,
            rewards,
            next_obs_list,
            next_possible_words_list,
            actual_n_list,
        ) = data
        batch_size = len(actual_n_list)

        input_quest, input_quest_char, _ = self.get_agent_inputs(quest_list)
        input_observation, input_observation_char, _ = self.get_agent_inputs(obs_list)
        next_input_observation, next_input_observation_char, _ = self.get_agent_inputs(
            next_obs_list
        )

        # calculate ICM loss -- tldeda001
        if self.icm:
            input_quest, input_quest_char, _ = self.get_agent_inputs(quest_list)
            input_state, input_state_chars, _ = self.get_agent_inputs(obs_list)
            input_next_state, input_next_state_chars, _ = self.get_agent_inputs(
                next_obs_list
            )

            if self.freeze_encoding:
                with torch.no_grad():
                    encoded_states = self.get_match_representations(
                        input_state, input_state_chars, input_quest, input_quest_char
                    )
                    encoded_next_states = self.get_match_representations(
                        input_next_state,
                        input_next_state_chars,
                        input_quest,
                        input_quest_char,
                    )
            else:
                encoded_states = self.get_match_representations(
                    input_state, input_state_chars, input_quest, input_quest_char
                )
                encoded_next_states = self.get_match_representations(
                    input_next_state,
                    input_next_state_chars,
                    input_quest,
                    input_quest_char,
                )

            action_inputs = torch.stack(action_list)

            encoded_actions, _ = self.online_net.word_embedding(action_inputs)

            if self.online_net.curiosity_module.beta == 0:
                icm_loss = (
                    self.online_net.curiosity_module.get_inverse_loss(
                        encoded_states, action_inputs, encoded_next_states
                    )
                ).mean()
            elif self.online_net.curiosity_module.beta == 1:
                icm_loss = (
                    self.online_net.curiosity_module.get_forward_loss(
                        encoded_states, encoded_actions, encoded_next_states
                    )
                ).mean()
            else:
                inverse_loss = (
                    self.online_net.curiosity_module.get_inverse_loss(
                        encoded_states, action_inputs, encoded_next_states
                    )
                ).mean()
                forward_loss = (
                    self.online_net.curiosity_module.get_forward_loss(
                        encoded_states, encoded_actions, encoded_next_states
                    )
                ).mean()
                icm_loss = (
                    (1 - self.online_net.curiosity_module.beta) * inverse_loss
                    + self.online_net.curiosity_module.beta * forward_loss
                )

        possible_words, next_possible_words = [], []
        for i in range(3):
            possible_words.append([item[i] for item in possible_words_list])
            next_possible_words.append([item[i] for item in next_possible_words_list])

        local_word_masks = [
            to_pt(item, self.use_cuda, type="float")
            for item in self.get_local_word_masks(possible_words)
        ]
        next_local_word_masks = [
            to_pt(item, self.use_cuda, type="float")
            for item in self.get_local_word_masks(next_possible_words)
        ]

        action_ranks = self.get_ranks(
            input_observation,
            input_observation_char,
            input_quest,
            input_quest_char,
            local_word_masks,
            use_model="online",
        )  # list of batch x vocab or list of batch x vocab x atoms
        # ps_a
        word_qvalues = [
            ez_gather_dim_1(w_rank, idx.unsqueeze(-1)).squeeze(1)
            for w_rank, idx in zip(action_ranks, chosen_indices)
        ]  # list of batch or list of batch x atoms
        # batch or batch x atoms
        q_value = torch.mean(torch.stack(word_qvalues, -1), -1)
        # log_ps_a
        log_q_value = torch.log(q_value)  # batch or batch x atoms

        with torch.no_grad():
            if self.noisy_net:
                self.target_net.reset_noise()  # Sample new target net noise
            if self.double_dqn:
                # pns Probabilities p(s_t+n, ·; θonline)
                next_action_ranks = self.get_ranks(
                    next_input_observation,
                    next_input_observation_char,
                    input_quest,
                    input_quest_char,
                    next_local_word_masks,
                    use_model="online",
                )
                # list of batch x vocab or list of batch x vocab x atoms
                # Perform argmax action selection using online network: argmax_a[(z, p(s_t+n, a; θonline))]
                next_word_indices = self.choose_maxQ_command(
                    next_action_ranks, next_local_word_masks
                )  # list of batch x 1
                # pns # Probabilities p(s_t+n, ·; θtarget)
                next_action_ranks = self.get_ranks(
                    next_input_observation,
                    next_input_observation_char,
                    input_quest,
                    input_quest_char,
                    next_local_word_masks,
                    use_model="target",
                )  # batch x vocab or list of batch x vocab x atoms
                # pns_a # Double-Q probabilities p(s_t+n, argmax_a[(z, p(s_t+n, a; θonline))]; θtarget)
                next_word_qvalues = [
                    ez_gather_dim_1(w_rank, idx.unsqueeze(-1)).squeeze(1)
                    for w_rank, idx in zip(next_action_ranks, next_word_indices)
                ]  # list of batch or list of batch x atoms
            else:
                # pns Probabilities p(s_t+n, ·; θonline)
                next_action_ranks = self.get_ranks(
                    next_input_observation,
                    next_input_observation_char,
                    input_quest,
                    input_quest_char,
                    next_local_word_masks,
                    use_model="target",
                )
                # list of batch x vocab or list of batch x vocab x atoms
                next_word_indices = self.choose_maxQ_command(
                    next_action_ranks, next_local_word_masks
                )  # list of batch x 1
                next_word_qvalues = [
                    ez_gather_dim_1(w_rank, idx.unsqueeze(-1)).squeeze(1)
                    for w_rank, idx in zip(next_action_ranks, next_word_indices)
                ]  # list of batch or list of batch x atoms

            next_q_value = torch.mean(
                torch.stack(next_word_qvalues, -1), -1
            )  # batch or batch x atoms
            # Compute Tz (Bellman operator T applied to z)
            discount = to_pt(
                (np.ones_like(actual_n_list) * self.discount_gamma) ** actual_n_list,
                self.use_cuda,
                type="float",
            )
        if not self.use_distributional:
            rewards = rewards + next_q_value * discount  # batch
            dqn_loss = F.smooth_l1_loss(q_value, rewards)

            if self.icm:
                loss = (
                    self.online_net.curiosity_module.lambda_weight * dqn_loss
                    + self.online_net.curiosity_module.loss_weight * icm_loss
                )
            else:
                loss = dqn_loss
            return loss

        with torch.no_grad():
            # Tz = R^n + (γ^n)z (accounting for terminal states)
            Tz = rewards.unsqueeze(-1) + discount.unsqueeze(
                -1
            ) * self.support.unsqueeze(0)
            # Clamp between supported values
            Tz = Tz.clamp(min=self.v_min, max=self.v_max)
            # Compute L2 projection of Tz onto fixed support z
            b = (Tz - self.v_min) / self.delta_z  # b = (Tz - Vmin) / Δz
            l, u = b.floor().to(torch.int64), b.ceil().to(torch.int64)
            # Fix disappearing probability mass when l = b = u (b is int)
            l[(u > 0) * (l == u)] -= 1
            u[(l < (self.atoms - 1)) * (l == u)] += 1

            # Distribute probability of Tz
            m = torch.zeros(
                batch_size, self.atoms, dtype=torch.float16, device=self.device
            ).float()

            offset = (
                torch.linspace(
                    0, ((batch_size - 1) * self.atoms), batch_size, device=self.device
                )
                .unsqueeze(1)
                .expand(batch_size, self.atoms)
                .long()
            )

            m.view(-1).index_add_(
                0, (l + offset).view(-1), (next_q_value * (u.float() - b)).view(-1)
            )  # m_l = m_l + p(s_t+n, a*)(u - b)
            m.view(-1).index_add_(
                0, (u + offset).view(-1), (next_q_value * (b - l.float())).view(-1)
            )  # m_u = m_u + p(s_t+n, a*)(b - l)

        # Cross-entropy loss (minimises DKL(m||p(s_t, a_t)))
        dqn_loss = -torch.sum(m * log_q_value, 1)
        dqn_loss = torch.mean(dqn_loss)
        if self.icm:
            loss = (
                self.online_net.curiosity_module.lambda_weight * dqn_loss
                + self.online_net.curiosity_module.loss_weight * icm_loss
            )
        else:
            loss = dqn_loss
        return loss

    def update_interaction(self):
        """
        Calculate the DQN loss, backprop to calculate the gradients and optimize the network.

        :return : the mean loss.
        """
        # update neural model by replaying snapshots in replay memory
        if self.a2c:
            interaction_loss = self.get_actor_critic_loss()
            self.command_generation_replay_memory.clear()
        else:
            interaction_loss = self.get_dqn_loss()

        if interaction_loss is None:
            return None
        loss = interaction_loss * self.interaction_loss_lambda

        # Backpropagate

        self.online_net.zero_grad()
        self.optimizer.zero_grad()

        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(
            self.online_net.parameters(), self.clip_grad_norm
        )
        self.optimizer.step()  # apply gradients
        return to_np(torch.mean(interaction_loss))

    def answer_question(
        self,
        input_observation,
        input_observation_char,
        observation_id_list,
        input_quest,
        input_quest_char,
        use_model="online",
    ):
        """
        Answer question based on observations.

        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param observation_id_list: list of observation strings
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.
        :param use_model: which model to use.

        :return vocab_distribution: distribution of probabilities over vocab.
        """
        # first pad answerer_input, and get the mask
        model = self.online_net if use_model == "online" else self.target_net
        batch_size = len(observation_id_list)
        max_length = input_observation.size(1)
        mask = compute_mask(input_observation)  # batch x obs_len

        # noun mask for location question
        if self.question_type in ["location"]:
            location_mask = []
            for i in range(batch_size):
                m = [1 for item in observation_id_list[i]]
                location_mask.append(m)
            location_mask = pad_sequences(
                location_mask, maxlen=max_length, dtype="float32"
            )
            location_mask = to_pt(
                location_mask, enable_cuda=self.use_cuda, type="float"
            )
            assert mask.size() == location_mask.size()
            mask = mask * location_mask

        match_representation_sequence = self.get_match_representations(
            input_observation,
            input_observation_char,
            input_quest,
            input_quest_char,
            use_model=use_model,
        )
        # batch x vocab or batch x 2
        pred = model.answer_question(match_representation_sequence, mask)

        # attention sum:
        # sometimes certain word appears multiple times in the observation,
        # thus we need to merge them together before doing further computations
        # ------- but
        # if answer type is not pointing, we just use a pre-defined mapping
        # that maps 0/1 to their positions in vocab
        if self.answer_type == "2 way":
            observation_id_list = []
            max_length = 2
            for i in range(batch_size):
                observation_id_list.append([self.word2id["0"], self.word2id["1"]])

        observation = to_pt(
            pad_sequences(observation_id_list, maxlen=max_length).astype("int32"),
            self.use_cuda,
        )
        vocab_distribution = np.zeros(
            (batch_size, len(self.word_vocab))
        )  # batch x vocab
        vocab_distribution = to_pt(vocab_distribution, self.use_cuda, type="float")
        vocab_distribution = vocab_distribution.scatter_add_(
            1, observation, pred
        )  # batch x vocab
        non_zero_words = []
        for i in range(batch_size):
            non_zero_words.append(list(set(observation_id_list[i])))
        vocab_mask = torch.ne(vocab_distribution, 0).float()

        return vocab_distribution, non_zero_words, vocab_mask

    def point_maxq_position(self, vocab_distribution, mask):
        """
        Generate a command by maximum q values, for epsilon greedy.

        Arguments:
            point_distribution: Q values for each position (mapped to vocab).
            mask: vocab masks.
        """
        vocab_distribution = (
            vocab_distribution
            - torch.min(vocab_distribution, -1, keepdim=True)[0]
            + 1e-2
        )  # minus the min value, so that all values are non-negative
        vocab_distribution = vocab_distribution * mask  # batch x vocab
        indices = torch.argmax(vocab_distribution, -1)  # batch
        return indices

    def answer_question_act_greedy(
        self,
        input_observation,
        input_observation_char,
        observation_id_list,
        input_quest,
        input_quest_char,
    ):
        """
        Answer the question.

        :param input_observation: observation strings processed into pytorch tensor.
        :param input_observation_char: observation chars processed into pytorch tensor.
        :param observation_id_list: list of observation strings
        :param input_quest: questions processed into pytorch tensor.
        :param input_quest_char: questions chars processed into pytorch tensor.

        :return positions_maxq: answer indices.
        """

        with torch.no_grad():
            vocab_distribution, _, vocab_mask = self.answer_question(
                input_observation,
                input_observation_char,
                observation_id_list,
                input_quest,
                input_quest_char,
                use_model="online",
            )  # batch x time
            positions_maxq = self.point_maxq_position(vocab_distribution, vocab_mask)
            return positions_maxq  # batch

    def get_qa_loss(self):
        """
        Update neural model in agent. In this example we follow algorithm
        of updating model in dqn with replay memory.

        :return mean loss: the mean loss of the batch.
        """
        if len(self.qa_replay_memory) < self.replay_batch_size:
            return None
        transitions = self.qa_replay_memory.sample(self.replay_batch_size)
        batch = qa_memory.qa_Transition(*zip(*transitions))

        observation_list = batch.observation_list
        quest_list = batch.quest_list
        answer_strings = batch.answer_strings
        answer_position = np.array(_words_to_ids(answer_strings, self.word2id))
        groundtruth = to_pt(answer_position, self.use_cuda)  # batch

        input_quest, input_quest_char, _ = self.get_agent_inputs(quest_list)

        (
            input_observation,
            input_observation_char,
            observation_id_list,
        ) = self.get_agent_inputs(observation_list)

        answer_distribution, _, _ = self.answer_question(
            input_observation,
            input_observation_char,
            observation_id_list,
            input_quest,
            input_quest_char,
            use_model="online",
        )  # batch x vocab

        batch_loss = NegativeLogLoss(answer_distribution, groundtruth)  # batch
        return torch.mean(batch_loss)

    def update_qa(self):
        """
        calculate gradients and backprop for QA module.

        :return mean loss: the mean loss.
        """
        # update neural model by replaying snapshots in replay memory
        qa_loss = self.get_qa_loss()
        if qa_loss is None:
            return None
        loss = qa_loss * self.qa_loss_lambda
        # Backpropagate
        self.online_net.zero_grad()
        self.optimizer.zero_grad()
        loss.backward()
        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm_(
            self.online_net.parameters(), self.clip_grad_norm
        )
        self.optimizer.step()  # apply gradients
        return to_np(torch.mean(qa_loss))

    def finish_of_episode(self, episode_no, batch_size):
        """
        Finish the episode and perform routine tasks such as checking if its time to update target network and reduce epsilon for epsilon greedy.

        :param episode_no: the episode number.
        :param batch_size: the size of the batch of games being played concurrently.
        """
        # Update target networt
        if (
            (episode_no + batch_size) % self.target_net_update_frequency
            <= episode_no % self.target_net_update_frequency
            and not self.a2c
        ):
            self.update_target_net()
        # decay lambdas
        if episode_no < self.learn_start_from_this_episode:
            return
        if (
            episode_no
            < self.epsilon_anneal_episodes + self.learn_start_from_this_episode
        ):
            self.epsilon -= (self.epsilon_anneal_from - self.epsilon_anneal_to) / float(
                self.epsilon_anneal_episodes
            )
            self.epsilon = max(self.epsilon, 0.0)
        if (
            episode_no
            < self.revisit_counting_lambda_anneal_episodes
            + self.learn_start_from_this_episode
        ):
            self.revisit_counting_lambda -= (
                self.revisit_counting_lambda_anneal_from
                - self.revisit_counting_lambda_anneal_to
            ) / float(self.revisit_counting_lambda_anneal_episodes)
            self.revisit_counting_lambda = max(self.epsilon, 0.0)

    def reset_binarized_counter(self, batch_size):
        """
        Reset the unique state counter.
        """
        self.binarized_counter_dict = [{} for _ in range(batch_size)]

    def get_binarized_count(self, observation_strings, update=True):
        """
        for every new state visited, a reward is given - this is used to check if a state has been visited before.

        :param observation_strings: the observation strings for each game in batch.
        :param update: boolean to decide whether or not to update the dictionary of states visited.

        :return count_rewards: list of rewards for the games in batch of wether or not the state visited is new. will always only be 1 or 0.
        """
        count_rewards = []
        batch_size = len(observation_strings)
        for i in range(batch_size):
            key = observation_strings[i]
            if key not in self.binarized_counter_dict[i]:
                self.binarized_counter_dict[i][key] = 0.0
            if update:
                self.binarized_counter_dict[i][key] += 1.0
            r = self.binarized_counter_dict[i][key]
            r = float(r == 1.0)
            count_rewards.append(r)
        return count_rewards
