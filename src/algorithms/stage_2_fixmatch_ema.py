import os
from os import sep
import math
import numpy as np
from datetime import datetime
from tqdm import tqdm
import copy
from collections import OrderedDict

import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.optim.lr_scheduler import LambdaLR               
               
from .utils import register_algorithm
from src.data.utils import load_dataset
from src.data.class_indices import class_indices
from src.models.utils import get_model
from src.algorithms.stage_1_plain import PlainStage1
from src.algorithms.stage_2_fixmatch import load_data, FixMatchStage2


@register_algorithm('EMAFixMatchStage2')
class EMAFixMatchStage2(FixMatchStage2):

    """
    Overall training function.
    """

    name = 'EMAFixMatchStage2'
    net = None
    opt_net = None
    scheduler = None

    def __init__(self, args):
        super(EMAFixMatchStage2, self).__init__(args=args)

    def set_train(self):
        ###########################
        # Setup cuda and networks #
        ###########################
        # setup network
        self.logger.info('\nGetting {} model.'.format(self.args.model_name))
        self.net = get_model(name=self.args.model_name, num_cls=len(class_indices[self.args.class_indices]),
                             weights_init=self.args.weights_init, num_layers=self.args.num_layers, init_feat_only=False,
                             parallel=True)

        self.set_optimizers()

        if self.args.ema:
            self.logger.info('\nGetting EMA model.')
            self.feature_ema = ModelEMA(self.args.lr_feature, self.args.weight_decay_feature,
                                        self.net.feature, decay=0.999)
            self.classifier_ema = ModelEMA(self.args.lr_classifier, self.args.weight_decay_classifier,
                                           self.net.classifier, decay=0.999)

    def set_eval(self):
        ###############################
        # Load weights for evaluation #
        ###############################
        self.logger.info('\nGetting {} model.'.format(self.args.model_name))
        self.logger.info('\nLoading from {}'.format(self.weights_path))
        self.net = get_model(name=self.args.model_name, num_cls=len(class_indices[self.args.class_indices]),
                             weights_init=self.weights_path, num_layers=self.args.num_layers, init_feat_only=False)

        if self.args.ema:
            self.logger.info('\nLoading EMA model from {}.'.format(self.weights_path.replace('.pth', '_ema.pth')))
            self.feature_ema = ModelEMA(self.args.lr_feature, self.args.weight_decay_feature,
                                        self.net.feature, decay=0.999)
            self.classifier_ema = ModelEMA(self.args.lr_classifier, self.args.weight_decay_classifier,
                                           self.net.classifier, decay=0.999)
            checkpoint = torch.load(self.weights_path.replace('.pth', '_ema.pth'))
            self.feature_ema.ema.load_state_dict(checkpoint['feature_ema'])
            self.classifier_ema.ema.load_state_dict(checkpoint['classifier_ema'])

    def train(self):

        best_epoch = 0
        best_acc = 0.

        for epoch in range(self.num_epochs):

            # Training
            self.train_epoch(epoch)

            # Validation
            self.logger.info('\nValidation.')
            val_acc_mac = self.evaluate(self.valloader, ood=False)
            if val_acc_mac > best_acc:
                self.logger.info('\nUpdating Best Model Weights!!')
                self.net.update_best()
                best_acc = val_acc_mac
                best_epoch = epoch
                self.save_ema_model()

        self.logger.info('\nBest Model Appears at Epoch {}...'.format(best_epoch))
        self.save_model()

    def train_epoch(self, epoch):

        self.net.train()

        loader_l = self.trainloader_l
        loader_u = self.trainloader_u

        iter_l = iter(loader_l)
        iter_u = iter(loader_u)

        N = self.train_iterations 

        for batch_idx in range(N):

            # log basic adda train info
            info_str = '[Train FixMatch (Stage 2)] '
            info_str += 'Epoch: {} [{}/{} ({:.2f}%)] '.format(epoch, batch_idx,
                                                              N, 100 * batch_idx / N)

            ########################
            # Setup data variables #
            ########################

            data_l, labels_l = next(iter_l)

            batch_size = data_l.shape[0]

            try:
                (data_u_w, data_u_s), labels_u = next(iter_u)
            except StopIteration:
                iter_u = iter(loader_u)
                (data_u_w, data_u_s), labels_u = next(iter_u)

            data = torch.cat((data_l, data_u_w, data_u_s)).cuda()
            labels_l, labels_u = labels_l.cuda(), labels_u.cuda()
            data.requires_grad = False
            labels_l.requires_grad = False
            labels_u.requires_grad = False

            ####################
            # Forward and loss #
            ####################
            # forward
            feats = self.net.feature(data)
            logits = self.net.classifier(feats)
            # split logits
            logits_l = logits[:batch_size]
            logits_u_w, logits_u_s = logits[batch_size:].chunk(2)
            del logits

            # Pseudo labels
            pseudo_label = torch.softmax(logits_u_w.detach_(), dim=-1)
            max_probs, targets_u = torch.max(pseudo_label, dim=-1)
            mask = max_probs.ge(self.args.fixmatch_t).float()

            # calculate loss
            loss_l = self.net.criterion_cls(logits_l, labels_l)
            loss_u = (F.cross_entropy(logits_u_s, targets_u, reduction='none') * mask).mean()

            loss = loss_l + self.args.fixmatch_lambda * loss_u
            
            #############################
            # Backward and optimization #
            #############################
            # zero gradients for optimizer
            self.opt_net.zero_grad()
            # loss backpropagation
            loss.backward()
            # optimize step
            self.opt_net.step()
            self.scheduler.step()

            if self.args.ema:
                self.feature_ema.update(self.net.feature)
                self.classifier_ema.update(self.net.classifier)

            ###########
            # Logging #
            ###########
            if batch_idx % self.log_interval == 0:
                # compute overall acc
                preds_l = logits_l.argmax(dim=1)
                acc_l = (preds_l == labels_l).float().mean()
                preds_u = logits_u_w.argmax(dim=1)
                acc_u = (preds_u == labels_u).float().mean()
                # log update info
                info_str += 'Acc_l: {:0.1f} Acc_u: {:0.1f} '.format(acc_l.item() * 100, acc_u.item() * 100)
                info_str += 'Xent_l: {:.3f} Xent_u: {:.3f}'.format(loss_l.item(), loss_u.item())
                self.logger.info(info_str)

    def evaluate_forward(self, loader, ood=False):
        total_preds = []
        total_labels = []
        total_logits = []

        # Forward and record # correct predictions of each class
        with torch.set_grad_enabled(False):

            for data, labels in tqdm(loader, total=len(loader)):

                # setup data
                data, labels = data.cuda(), labels.cuda()
                data.requires_grad = False
                labels.requires_grad = False

                # forward
                if self.args.ema:
                    feats = self.feature_ema.ema(data)
                    logits = self.classifier_ema.ema(feats)
                else:
                    feats = self.net.feature(data)
                    logits = self.net.classifier(feats)

                max_probs, preds = F.softmax(logits, dim=1).max(dim=1)

                # Set unconfident prediction to -1
                if ood:
                    preds[max_probs < self.args.theta] = -1

                total_preds.append(preds.detach().cpu().numpy())
                total_labels.append(labels.detach().cpu().numpy())
                total_logits.append(logits.detach().cpu().numpy())

        return total_preds, total_labels, total_logits

    def save_ema_model(self):
        os.makedirs(self.weights_path.rsplit('/', 1)[0], exist_ok=True)
        self.logger.info('Saving EMA model to {}'.format(self.weights_path.replace('.pth', '_ema.pth')))

        ema_states = {
            'feature_ema': self.feature_ema.ema.module.state_dict() \
                           if self.feature_ema.ema_has_module else self.feature_ema.ema.module.state_dict(),
            'classifier_ema': self.classifier_ema.ema.state_dict() \
                              if self.classifier_ema.ema_has_module else self.classifier_ema.ema.module.state_dict(),
        }
        
        torch.save(ema_states, self.weights_path.replace('.pth', '_ema.pth'))
        

class ModelEMA(object):
    def __init__(self, lr, wdecay, model, decay):
        self.ema = copy.deepcopy(model).cuda()
        self.ema.eval()
        self.decay = decay
        self.wd = lr * wdecay
        self.ema_has_module = hasattr(self.ema, 'module')
        for p in self.ema.parameters():
            p.requires_grad_(False)

    def update(self, model):
        needs_module = hasattr(model, 'module') and not self.ema_has_module
        with torch.no_grad():
            msd = model.state_dict()
            for k, ema_v in self.ema.state_dict().items():
                if needs_module:
                    k = 'module.' + k
                model_v = msd[k].detach().cuda()
                ema_v.copy_(ema_v * self.decay + (1. - self.decay) * model_v)
                # weight decay
                if 'bn' not in k:
                    msd[k] = msd[k] * (1. - self.wd)