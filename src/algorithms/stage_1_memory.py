import os
import numpy as np
from datetime import datetime
from tqdm import tqdm

import torch
import torch.optim as optim
import torch.nn.functional as F

from .utils import register_algorithm, stage_1_metric
from src.data.class_indices import class_indices
from src.algorithms.plain_stage_1 import PlainStage1

@register_algorithm('MemoryStage1')
class MemoryStage1(PlainStage1):

    """
    Overall training function.
    """

    name = 'MemoryStage1'
    net = None
    opt_net = None
    scheduler = None
    centroids = None

    def __init__(self, args):
        super(MemoryStage1, self).__init__(args=args)

    def deploy_epoch(self, loader):

        self.net.eval()

        # Get unique classes in the loader and corresponding counts
        loader_uni_class, eval_class_counts = loader.dataset.class_counts_cal()

        total_logits = []
        total_preds = []
        total_labels = []

        # Forward and record # correct predictions of each class
        with torch.set_grad_enabled(False):

            for data, labels in tqdm(loader, total=len(loader)):

                # setup data
                data, labels = data.cuda(), labels.cuda()
                data.requires_grad = False
                labels.requires_grad = False

                # forward
                feats = self.net.feature(data)
                logits = self.net.classifier(feats)

                # Reachability
                # expand dimension
                feats_expand = feats.clone().unsqueeze(1).expand(-1, len(self.centroids), -1)
                centroids_expand = self.centroids.clone().unsqueeze(0).expand(len(data), -1, -1)
                # computing reachability
                dist_to_centroids = torch.norm(feats_expand - centroids_expand, 2, 2)
                # Sort distances
                values_nn, labels_nn = torch.sort(dist_to_centroids, 1)
                # expand to logits dimension and scale the smallest distance
                reachability = (self.args.reachability_scale / values_nn[:, 0]).unsqueeze(1).expand(-1, logits.shape[1])
                # scale logits with reachability
                logits = reachability * logits

                # Prediction
                max_probs, preds = F.softmax(logits, dim=1).max(dim=1)

                # Set unconfident prediction to -1
                preds[max_probs < self.args.theta] = -1

                total_logits.append(logits.detach().cpu().numpy())
                total_preds.append(preds.detach().cpu().numpy())
                total_labels.append(labels.detach().cpu().numpy())

        f1,\
        class_acc_confident, class_percent_confident, false_pos_percent, \
        class_wrong_percent_unconfident, \
        percent_unknown, conf_preds = stage_1_metric(np.concatenate(total_preds, axis=0),
                                                     np.concatenate(total_labels, axis=0),
                                                     loader_uni_class,
                                                     eval_class_counts)

        eval_info = '{} Per-class evaluation results: \n'.format(datetime.now().strftime("%Y-%m-%d_%H:%M:%S"))

        for i in range(len(class_acc_confident)):
            # TODO change loader_uni_class behavior
            eval_info += 'Class {} (train counts {}):'.format(i, self.train_class_counts[loader_uni_class[loader_uni_class != -1]][i])
            eval_info += 'Confident percentage: {:.2f}; '.format(class_percent_confident[i] * 100)
            eval_info += 'Unconfident wrong %: {:.2f}; '.format(class_wrong_percent_unconfident[i] * 100)
            eval_info += 'Accuracy: {:.3f} \n'.format(class_acc_confident[i] * 100)

        eval_info += 'Overall F1: {:.3f} \n'.format(f1)
        eval_info += 'False positive percentage: {:.3f} \n'.format(false_pos_percent * 100)
        eval_info += 'Selected unknown percentage: {:.3f} \n'.format(percent_unknown * 100)

        eval_info += 'Avg conf %: {:.3f}; \n'.format(class_percent_confident.mean() * 100)
        eval_info += 'Avg unconf wrong %: {:.3f}; \n'.format(class_wrong_percent_unconfident.mean() * 100)
        eval_info += 'Conf acc %: {:.3f}\n'.format(class_acc_confident.mean() * 100)

        return eval_info, f1, conf_preds, np.concatenate(total_preds, axis=0), np.concatenate(total_logits, axis=0)

    def centroids_cal(self, loader):

        self.net.eval()

        centroids = torch.zeros(len(class_indices[self.args.class_indices]),
                                self.net.feature_dim).cuda()

        with torch.set_grad_enabled(False):
            for data, labels in tqdm(loader, total=len(loader)):
                # setup data
                data, labels = data.cuda(), labels.cuda()
                data.requires_grad = False
                labels.requires_grad = False
                # forward
                feats = self.net.feature(data)
                # Add all calculated features to center tensor
                for i in range(len(labels)):
                    label = labels[i]
                    centroids[label] += feats[i]

        # Get data counts
        _, loader_class_counts = loader.dataset.class_counts_cal()
        # Average summed features with class count
        centroids /= torch.tensor(loader_class_counts).float().unsqueeze(1).cuda()

        return centroids

    def deploy(self, loader):

        # Calculate training data centroids first
        centroids_path = self.weights_path.replace('.pth', '_centroids.npy')
        if os.path.exists(centroids_path):
            self.logger.info('Loading centroids from {}.\n'.format(centroids_path))
            cent_np = np.fromfile(centroids_path, dtype=np.float32).reshape(-1, self.net.feature_dim)
            self.centroids = torch.from_numpy(cent_np).cuda()
        else:
            self.logger.info('Calculating training data centroids.\n')
            self.centroids = self.centroids_cal(self.trainloader)
            self.centroids.clone().detach().cpu().numpy().tofile(centroids_path)
            self.logger.info('Centroids saved to {}.\n'.format(centroids_path))

        # Evaluate
        eval_info, f1, conf_preds, init_pseudo_hard, init_pseudo_soft = self.deploy_epoch(loader)

        self.logger.info(eval_info)

        conf_preds_path = self.weights_path.replace('.pth', '_conf_preds.npy')
        self.logger.info('Saving confident predictions to {}'.format(conf_preds_path))
        conf_preds.tofile(conf_preds_path)

        if self.args.soft == 1:

            init_pseudo_hard_path = self.weights_path.replace('.pth', '_init_pseudo_hard.npy')
            self.logger.info('Saving initial hard pseudo labels to {}'.format(init_pseudo_hard_path))
            init_pseudo_hard.tofile(init_pseudo_hard_path)

            init_pseudo_soft_path = self.weights_path.replace('.pth', '_init_pseudo_soft.npy')
            self.logger.info('Saving initial soft pseudo targets to {}'.format(init_pseudo_soft_path))
            init_pseudo_soft.tofile(init_pseudo_soft_path)

        else:
            init_pseudo_path = self.weights_path.replace('.pth', '_init_pseudo.npy')
            self.logger.info('Saving initial pseudo labels to {}'.format(init_pseudo_path))
            init_pseudo_hard.tofile(init_pseudo_path)

        return f1





