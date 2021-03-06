# coding=utf-8
# Copyright (c) Microsoft. All rights reserved.
import logging

import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.nn.utils
from torch.autograd import Variable
from torch.optim.lr_scheduler import *
from data_utils.utils import AverageMeter
from bert.optimization import BertAdam as Adam
from module.bert_optim import Adamax
from module.my_optim import EMA
from .matcher_chr import SANChrBertNetwork
import pdb
from .batcher import mediqa_name_list

logger = logging.getLogger(__name__)

class ChrMTDNNModel(object):
    def __init__(self, opt, state_dict=None, num_train_step=-1):
        self.config = opt
        self.updates = state_dict['updates'] if state_dict and 'updates' in state_dict else 0
        self.train_loss = AverageMeter()
        self.network = SANChrBertNetwork(opt)

        # pdb.set_trace()
        if state_dict:
            new_state = set(self.network.state_dict().keys())
            # change to a safer approach
            old_keys=[k for k in state_dict['state'].keys()]
            for k in old_keys:
                if k not in new_state:
                    print('deleting state:',k)
                    del state_dict['state'][k]
            for k, v in list(self.network.state_dict().items()):
                if k not in state_dict['state']:
                    print('adding missing state:',k)
                    state_dict['state'][k] = v
            # pdb.set_trace()
            self.network.load_state_dict(state_dict['state'])
        self.mnetwork = nn.DataParallel(self.network) if opt['multi_gpu_on'] else self.network
        self.total_param = sum([p.nelement() for p in self.network.parameters() if p.requires_grad])

        no_decay = ['bias', 'gamma', 'beta', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_parameters = [
            {'params': [p for n, p in self.network.named_parameters() if n not in no_decay], 'weight_decay_rate': 0.01},
            {'params': [p for n, p in self.network.named_parameters() if n in no_decay], 'weight_decay_rate': 0.0}
            ]
        # note that adamax are modified based on the BERT code
        if opt['optimizer'] == 'sgd':
            self.optimizer = optim.SGD(optimizer_parameters, opt['learning_rate'],
                                       weight_decay=opt['weight_decay'])

        elif opt['optimizer'] == 'adamax':
            self.optimizer = Adamax(optimizer_parameters,
                                        opt['learning_rate'],
                                        warmup=opt['warmup'],
                                        t_total=num_train_step,
                                        max_grad_norm=opt['grad_clipping'],
                                        schedule=opt['warmup_schedule'])
            if opt.get('have_lr_scheduler', False): opt['have_lr_scheduler'] = False
        elif opt['optimizer'] == 'adadelta':
            self.optimizer = optim.Adadelta(optimizer_parameters,
                                            opt['learning_rate'],
                                            rho=0.95)
        elif opt['optimizer'] == 'adam':
            self.optimizer = Adam(optimizer_parameters,
                                        lr=opt['learning_rate'],
                                        warmup=opt['warmup'],
                                        t_total=num_train_step,
                                        max_grad_norm=opt['grad_clipping'],
                                        schedule=opt['warmup_schedule'])
            if opt.get('have_lr_scheduler', False): opt['have_lr_scheduler'] = False
        else:
            raise RuntimeError('Unsupported optimizer: %s' % opt['optimizer'])

        if state_dict and 'optimizer' in state_dict:
            self.optimizer.load_state_dict(state_dict['optimizer'])

        if opt.get('have_lr_scheduler', False):
            if opt.get('scheduler_type', 'rop') == 'rop':
                self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=opt['lr_gamma'], patience=3)
            elif opt.get('scheduler_type', 'rop') == 'exp':
                self.scheduler = ExponentialLR(self.optimizer, gamma=opt.get('lr_gamma', 0.95))
            else:
                milestones = [int(step) for step in opt.get('multi_step_lr', '10,20,30').split(',')]
                self.scheduler = MultiStepLR(self.optimizer, milestones=milestones, gamma=opt.get('lr_gamma'))
        else:
            self.scheduler = None
        self.ema = None
        if opt['ema_opt'] > 0:
            self.ema = EMA(self.config['ema_gamma'], self.network)
        self.para_swapped=False

    def setup_ema(self):
        if self.config['ema_opt']:
            self.ema.setup()

    def update_ema(self):
        if self.config['ema_opt']:
            self.ema.update()

    def eval(self):
        if self.config['ema_opt']:
            self.ema.swap_parameters()
            self.para_swapped = True

    def train(self):
        if self.para_swapped:
            self.ema.swap_parameters()
            self.para_swapped = False

    def update(self, batch_meta, batch_data):
        self.network.train()
        labels = batch_data[batch_meta['label']]
        # print('data size:',batch_data[batch_meta['token_id']].size())
        if batch_meta['pairwise']:
            labels = labels.contiguous().view(-1, batch_meta['pairwise_size'])[:, 0]
        if self.config['cuda']:
            y = Variable(labels.cuda(non_blocking=True), requires_grad=False)
        else:
            y = Variable(labels, requires_grad=False)
        task_id = batch_meta['task_id']
        task_type = batch_meta['task_type']
        inputs = batch_data[:batch_meta['input_len']]
        if len(inputs) == 3:
            inputs.append(None)
            inputs.append(None)
        inputs.append(task_id)
        # pdb.set_trace()
        #print("inputs", inputs)
        logits = self.mnetwork(*inputs)
        if batch_meta['pairwise']:
            logits = logits.view(-1, batch_meta['pairwise_size'])


        # pdb.set_trace()
        if task_type > 0:
            if self.config['answer_relu']:
                logits=F.relu(logits)
            loss = F.mse_loss(logits.squeeze(1), y)
        else:
            loss = F.cross_entropy(logits, y)


        if self.config['mediqa_pairloss'] is not None and batch_meta['dataset_name'] in mediqa_name_list:
            # print(logits)
            # print(batch_data[batch_meta['rank_label']].size())
            # input('ha')
            logits=logits.squeeze().view(-1, 2)
            # print(batch_data[batch_meta['rank_label']])
            rank_y = batch_data[batch_meta['rank_label']].view(-1,2)
            # print(rank_y)
            if self.config['mediqa_pairloss']=='hinge':
                # print(logits)
                first_logit, second_logit = logits.split(1,dim=1)
                # print(first_logit,second_logit)
                # pdb.set_trace()
                rank_y = (2*rank_y-1).to(torch.float32)
                rank_y = rank_y[:,0]
                pairwise_loss = F.margin_ranking_loss(first_logit.squeeze(1), second_logit.squeeze(1), rank_y, 
                    margin=self.config['hinge_lambda'])
            else:
                # pdb.set_trace()
                pairwise_loss = F.cross_entropy(logits,rank_y[:,1])
            # print('pairwise_loss:',pairwise_loss,'mse loss:',loss)
            loss += pairwise_loss

        self.train_loss.update(loss.item(), logits.size(0))
        self.optimizer.zero_grad()

        loss.backward()
        if self.config['global_grad_clipping'] > 0:
            torch.nn.utils.clip_grad_norm_(self.network.parameters(),
                                          self.config['global_grad_clipping'])
        self.optimizer.step()
        self.updates += 1
        self.update_ema()

    def predict(self, batch_meta, batch_data):
        self.network.eval()
        task_id = batch_meta['task_id']
        task_type = batch_meta['task_type']
        inputs = batch_data[:batch_meta['input_len']]
        if len(inputs) == 3:
            inputs.append(None)
            inputs.append(None)
        inputs.append(task_id)
        score = self.mnetwork(*inputs)
        gold_label = batch_meta['label']
        if batch_meta['pairwise']:
            score = score.contiguous().view(-1, batch_meta['pairwise_size'])
            if task_type < 1:
                score = F.softmax(score, dim=1)
            score = score.data.cpu()
            score = score.numpy()
            predict = np.zeros(score.shape, dtype=int)
            if task_type < 1:
                positive = np.argmax(score, axis=1)
                for idx, pos in enumerate(positive):
                    predict[idx, pos] = 1
            predict = predict.reshape(-1).tolist()
            score = score.reshape(-1).tolist()
            return score, predict, batch_meta['true_label']
        else:
            if task_type < 1:
                score = F.softmax(score, dim=1)
                # pdb.set_trace()
            score = score.data.cpu()
            score = score.numpy()
            if task_type < 1:
                predict = np.argmax(score, axis=1).tolist()
            else:
                predict = np.greater(score, 2.0+self.config['mediqa_score_offset']).astype(int)
                gold_label = np.greater(batch_meta['label'], 2.00001+self.config['mediqa_score_offset']).astype(int)
                predict = predict.reshape(-1).tolist()
                gold_label = gold_label.reshape(-1).tolist()
                # print('predict:',predict,score)

            score = score.reshape(-1).tolist()

        return score, predict, gold_label

    def save(self, filename):
        network_state = dict([(k, v.cpu()) for k, v in self.network.state_dict().items()])
        ema_state = dict(
            [(k, v.cpu()) for k, v in self.ema.model.state_dict().items()]) if self.ema is not None else dict()
        params = {
            'state': network_state,
            'optimizer': self.optimizer.state_dict(),
            'ema': ema_state,
            'config': self.config,
        }
        torch.save(params, filename)
        logger.info('model saved to {}'.format(filename))

    def cuda(self):
        self.network.cuda()
        if self.config['ema_opt']:
            self.ema.cuda()