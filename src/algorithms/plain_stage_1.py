import os
import numpy as np
from datetime import datetime
from tqdm import tqdm

import torch
import torch.optim as optim
import torch.nn.functional as F

from .utils import register_algorithm, Algorithm, stage_1_metric
from src.data.utils import load_dataset
from src.data.class_indices import class_indices
from src.models.utils import get_model


def load_data(args):

    """
    Dataloading function. This function can change alg by alg as well.
    """

    print('Using class indices: {} \n'.format(class_indices[args.class_indices]))

    cls_idx = class_indices[args.class_indices]

    trainloader = load_dataset(name=args.dataset_name,
                               class_indices=cls_idx,
                               dset='train',
                               transform='train',
                               split=args.train_split,
                               rootdir=args.dataset_root,
                               batch_size=args.batch_size,
                               shuffle=True,
                               num_workers=args.num_workers)

    # Use replace S1 to S2 for evaluation
    testloader = load_dataset(name=args.dataset_name.replace('S1', 'S2'),
                              class_indices=cls_idx,
                              dset='train',
                              transform='eval',
                              split=None,
                              rootdir=args.dataset_root,
                              batch_size=args.batch_size,
                              shuffle=False,
                              num_workers=args.num_workers)

    # Use replace S1 to S2 for evaluation
    valloader = load_dataset(name=args.dataset_name.replace('S1', 'S2'),
                             class_indices=cls_idx,
                             dset='train',
                             transform='eval',
                             split=None,
                             rootdir=args.dataset_root,
                             batch_size=args.batch_size,
                             shuffle=False,
                             num_workers=args.num_workers)

    return trainloader, testloader, valloader


@register_algorithm('PlainStage1')
class PlainStage1(Algorithm):

    """
    Overall training function.
    """

    name = 'PlainStage1'
    net = None
    opt_net = None
    scheduler = None

    def __init__(self, args):
        super(PlainStage1, self).__init__(args=args)

        # Training epochs and logging intervals
        self.num_epochs = args.num_epochs
        self.log_interval = args.log_interval

        #######################################
        # Setup data for training and testing #
        #######################################
        self.trainloader, self.testloader, self.valloader = load_data(args)
        _, self.train_class_counts = self.trainloader.dataset.class_counts_cal()

    def set_train(self):
        ###########################
        # Setup cuda and networks #
        ###########################
        # setup network
        self.logger.info('\nGetting {} model.'.format(self.args.model_name))
        self.net = get_model(name=self.args.model_name, num_cls=len(class_indices[self.args.class_indices]),
                             weights_init=self.args.weights_init, num_layers=self.args.num_layers, init_feat_only=True)

        ######################
        # Optimization setup #
        ######################
        # Setup optimizer parameters for each network component
        net_optim_params_list = [
            {'params': self.net.feature.parameters(),
             'lr': self.args.lr_feature,
             'momentum': self.args.momentum_feature,
             'weight_decay': self.args.weight_decay_feature},
            {'params': self.net.classifier.parameters(),
             'lr': self.args.lr_classifier,
             'momentum': self.args.momentum_classifier,
             'weight_decay': self.args.weight_decay_classifier}
        ]
        # Setup optimizer and optimizer scheduler
        self.opt_net = optim.SGD(net_optim_params_list)
        self.scheduler = optim.lr_scheduler.StepLR(self.opt_net, step_size=self.args.step_size, gamma=self.args.gamma)

    def set_eval(self):
        ###############################
        # Load weights for evaluation #
        ###############################
        self.logger.info('\nGetting {} model.'.format(self.args.model_name))
        self.logger.info('\nLoading from {}'.format(self.weights_path))
        self.net = get_model(name=self.args.model_name, num_cls=len(class_indices[self.args.class_indices]),
                             weights_init=self.weights_path, num_layers=self.args.num_layers, init_feat_only=False)

    def train_epoch(self, epoch):

        self.net.train()

        N = len(self.trainloader)

        for batch_idx, (data, labels) in enumerate(self.trainloader):

            # log basic adda train info
            info_str = '[Training {} - Stage 1] Epoch: {} [{}/{} ({:.2f}%)] '.format(self.net.name, epoch, batch_idx,
                                                                                     N, 100 * batch_idx / N)

            ########################
            # Setup data variables #
            ########################
            data, labels = data.cuda(), labels.cuda()

            data.require_grad = False
            labels.require_grad = False

            ####################
            # Forward and loss #
            ####################
            # forward
            feats = self.net.feature(data)
            logits = self.net.classifier(feats)
            # calculate loss
            loss = self.net.criterion_cls(logits, labels)

            #############################
            # Backward and optimization #
            #############################
            # zero gradients for optimizer
            self.opt_net.zero_grad()
            # loss backpropagation
            loss.backward()
            # optimize step
            self.opt_net.step()

            ###########
            # Logging #
            ###########
            if batch_idx % self.log_interval == 0:
                # compute overall acc
                preds = logits.argmax(dim=1)
                acc = (preds == labels).float().mean()
                # log update info
                info_str += 'Acc: {:0.1f} Xent: {:.3f}'.format(acc.item() * 100, loss.item())
                self.logger.info(info_str)

        self.scheduler.step()

    def train(self):

        best_f1 = 0.

        for epoch in range(self.num_epochs):

            # Training
            self.train_epoch(epoch)

            # Validation
            self.logger.info('\nValidation.')
            val_f1 = self.evaluate(self.valloader)
            if val_f1 > best_f1:
                self.net.update_best()

        self.save_model()

    def evaluate_epoch(self, loader):

        self.net.eval()

        # Get unique classes in the loader and corresponding counts
        loader_uni_class, eval_class_counts = loader.dataset.class_counts_cal()

        total_preds = []
        total_labels = []

        # Forward and record # correct predictions of each class
        with torch.set_grad_enabled(False):

            for data, labels in tqdm(loader, total=len(loader)):

                # setup data
                data, labels = data.cuda(), labels.cuda()
                data.require_grad = False
                labels.require_grad = False

                # forward
                feats = self.net.feature(data)
                logits = self.net.classifier(feats)

                max_probs, preds = F.softmax(logits, dim=1).max(dim=1)

                # Set unconfident prediction to -1
                preds[max_probs < self.args.theta] = -1

                total_preds.append(preds.detach().cpu().numpy())
                total_labels.append(labels.detach().cpu().numpy())

        f1,\
        class_acc_confident, class_percent_confident, false_pos_percent,\
        percent_unknown, conf_preds = stage_1_metric(np.concatenate(total_preds, axis=0),
                                                     np.concatenate(total_labels, axis=0),
                                                     loader_uni_class,
                                                     eval_class_counts)

        eval_info = '{} Per-class evaluation results: \n'.format(datetime.now().strftime("%Y-%m-%d_%H:%M:%S"))

        for i in range(len(class_acc_confident)):
            eval_info += 'Class {} (train counts {}):'.format(i, self.train_class_counts[i])
            eval_info += 'Confident percentage: {:.2f};'.format(class_percent_confident[i] * 100)
            eval_info += 'Accuracy: {:.3f} \n'.format(class_acc_confident[i] * 100)

        eval_info += 'Overall F1: {:.3f} \n'.format(f1)
        eval_info += 'False positive percentage: {:.3f} \n'.format(false_pos_percent * 100)
        eval_info += 'Selected unknown percentage: {:.3f} \n'.format(percent_unknown * 100)

        return eval_info, f1, conf_preds

    def evaluate(self, loader):
        eval_info, f1, conf_preds = self.evaluate_epoch(loader)
        self.logger.info(eval_info)

        if loader == self.testloader:
            conf_preds_path = self.weights_path.replace('.pth', '_conf_preds.npz')
            self.logger.info('Saving confident predictions to {}'.format(conf_preds_path))
            conf_preds.tofile(conf_preds_path)

        return f1

    def save_model(self):
        os.makedirs(self.weights_path.rsplit('/', 1)[0], exist_ok=True)
        self.logger.info('Saving to {}'.format(self.weights_path))
        self.net.save(self.weights_path)

