'''
The code is inpired by https://github.com/befelix/SafeOpt/tree/master
The random RKHS fucntion generation is inspired by https://github.com/Data-Science-in-Mechanical-Engineering/UncertaintyBounds21
Both corresponding works are cited in the main text.
---
Anonymous authors.
'''


import torch
import torch.nn as nn
import gpytorch
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from mpl_toolkits.mplot3d import Axes3D
import numpy as np
import math
from tqdm import tqdm
import time
import warnings
import copy
import multiprocessing
import tikzplotlib
import pickle
import dill
from matplotlib.patches import Ellipse
import torch.multiprocessing as mp
from scipy.special import comb
from plot import plot_1D, plot_2D_contour, plot_1D_SafeOpt_with_sets, plot_gym, plot_gym_together
from ground_truth_experiment import ground_truth_experiment
import gym
import sys
import os

# Uncomment the following and clone repo https://git.rwth-aachen.de/quanser-vision/vision-based-furuta-pendulum to conduct Furuta pendulum experiments

# sys.path.insert(1,  './vision-based-furuta-pendulum-master')
# from gym_brt.envs import QubeBalanceEnv, QubeSwingupEnv
# from gym_brt.control.control import QubeHoldControl, QubeFlipUpControl
# warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)
# from IPython import embed as IPS


class MultiLayerRNN(nn.Module):
    def __init__(self, hidden_size, num_layers, num_classes):
        super(MultiLayerRNN, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        # Define the first input branch RNN
        self.rnn1 = nn.LSTM(input_size=50, hidden_size=hidden_size, num_layers=num_layers, batch_first=True)
        # Define the second input branch RNN
        self.rnn2 = nn.LSTM(input_size=50, hidden_size=hidden_size, num_layers=num_layers, batch_first=True)
        # Merge layer
        self.merge_layer = nn.Linear(hidden_size * 2, hidden_size)
        # Output layer
        self.fc = nn.Linear(hidden_size, num_classes)

    def forward(self, x1, x2):
        # Forward pass for the first input branch
        out1, _ = self.rnn1(x1)
        # Forward pass for the second input branch
        out2, _ = self.rnn2(x2)
        # Concatenate the outputs of both branches
        out = torch.cat((out1, out2), dim=1)
        # Merge layer
        out = self.merge_layer(out)
        # Output layer
        out = self.fc(out)
        return out


def load_model(model_path, hidden_size, num_layers, num_classes):
    model = MultiLayerRNN(hidden_size=hidden_size, num_layers=num_layers, num_classes=num_classes)
    model.load_state_dict(torch.load(model_path))
    return model
def predict(model, input1, input2):
    model.eval()
    input1_tensor = torch.tensor(input1).unsqueeze(0)
    input2_tensor = torch.tensor(input2).unsqueeze(0)
    with torch.no_grad():
        output = model(input1_tensor, input2_tensor)
    return output.item()



class GPRegressionModel(gpytorch.models.ExactGP):  # this model has to be build "new"
    def __init__(self, train_x, train_y, noise_std, n_devices=1, output_device=torch.device('cpu'), lengthscale=0.1):
        likelihood = gpytorch.likelihoods.GaussianLikelihood()
        likelihood.noise = torch.tensor(noise_std**2)
        super(GPRegressionModel, self).__init__(train_x, train_y, likelihood)
        self.mean_module = gpytorch.means.ConstantMean()
        self.kernel = gpytorch.kernels.MaternKernel(nu=1.5)
        self.kernel.lengthscale = lengthscale
        # self.base_kernel.lengthscale.requires_grad = False; somehow does not work
        if output_device.type != 'cpu':
            self.covar_module = gpytorch.kernels.MultiDeviceKernel(
                self.kernel, device_ids=range(n_devices), output_device=output_device)
        else:
            self.covar_module = self.kernel

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)


def reshape_with_extra_dims(tensor, num_dims):
    # Calculate the number of extra dimensions needed
    extra_dims = [1] * (num_dims - tensor.dim())  # Adjust extra dimensions based on the tensor's shape
    # Reshape the tensor with extra dimensions
    reshaped_tensor = tensor.unsqueeze(*extra_dims).float()  # Convert tensor to floating-point
    return reshaped_tensor


def convert_to_hashable(item):
    if isinstance(item, (torch.Tensor, np.ndarray)):
        return tuple(map(tuple, item.tolist()))
    elif isinstance(item, tuple):
        return tuple(convert_to_hashable(i) for i in item)
    else:
        return item


def compute_X_plot(n_dimensions, points_per_axis):
    X_plot_per_domain = torch.linspace(0, 1, points_per_axis)
    X_plot_per_domain_nd = [X_plot_per_domain] * n_dimensions
    X_plot = torch.cartesian_prod(*X_plot_per_domain_nd).reshape(-1, n_dimensions)
    return X_plot


def initial_safe_samples(gt, num_safe_points):  # for toy examples and introductory example
    fX = gt.fX
    num_safe_points = num_safe_points
    # sampling_logic = fX > gt.safety_threshold  # alternative option
    sampling_logic = torch.logical_and(fX > np.quantile(fX, 0.4), fX < np.quantile(fX, 0.50))
    random_indices_sample = torch.randint(high=X_plot[sampling_logic].shape[0], size=(num_safe_points,))
    X_sample = X_plot[sampling_logic][random_indices_sample]
    Y_sample = fX[sampling_logic][random_indices_sample] + torch.tensor(np.random.normal(loc=0, scale=noise_std, size=X_sample.shape[0]), dtype=torch.float32)
    return X_sample, Y_sample


class ground_truth():
    def __init__(self, num_center_points, X_plot, RKHS_norm):
        def fun(kernel, alpha):
            return lambda X: kernel(X.reshape(-1, self.X_center.shape[1]), self.X_center).detach().numpy() @ alpha
        # For ground truth
        self.X_plot = X_plot
        self.RKHS_norm = RKHS_norm
        random_indices_center = torch.randint(high=self.X_plot.shape[0], size=(num_center_points,))
        self.X_center = self.X_plot[random_indices_center]
        alpha = np.random.uniform(-1, 1, size=self.X_center.shape[0])
        self.kernel = gpytorch.kernels.MaternKernel(nu=3/2)
        self.kernel.lengthscale = 0.1  # used in all runs except for Furuta hardware. There, this will get over-written.
        RKHS_norm_squared = alpha.T @ self.kernel(self.X_center, self.X_center).detach().numpy() @ alpha
        alpha /= np.sqrt(RKHS_norm_squared)/RKHS_norm  # scale to RKHS norm
        self.f = fun(self.kernel, alpha)
        self.fX = torch.tensor(self.f(self.X_plot), dtype=torch.float32)
        self.safety_threshold = np.quantile(self.fX, 0.3)  # np.quantile(self.fX, np.random.uniform(low=0.15, high=0.5))  # alternative option

    def conduct_experiment(self, x, noise_std):
        return torch.tensor(self.f(x) + np.random.normal(loc=0, scale=noise_std, size=1), dtype=x.dtype)

    def local_RKHS_norm(self, lb, ub, X_plot_local=None):  # needed for getting training labels
        nugget_factor = 1e-4  # regularization
        # Returns RKHS norm of ground truth on local domain between lb and ub; heuristic kernel interpolation approach
        if X_plot_local is None:
            local_gt_indices = torch.all(torch.logical_and(self.X_plot >= lb, self.X_plot <= ub), axis=1)
            if sum(local_gt_indices) > 10000:  # the problem is that the matrix gets very high dimensional and we cannot explicitly compute it
                subset_boolean = torch.randperm(sum(local_gt_indices)) < 10000
                X_local = self.X_plot[local_gt_indices][subset_boolean]
                fX_local = self.fX[local_gt_indices][subset_boolean]
            else:
                X_local = self.X_plot[local_gt_indices]
                fX_local = self.fX[local_gt_indices]
        else:  # use the local X_plots
            if X_plot.shape[0] > 10000:
                X_local = X_plot_local[torch.randperm(X_plot_local.shape[0]) < 10000]
                fX_local = torch.tensor(self.f(X_local), dtype=X_local.dtype)
            else:
                X_local = X_plot_local
                fX_local = torch.tensor(self.f(X_local), dtype=X_local.dtype)
        K_local = self.kernel(X_local, X_local).evaluate() + torch.eye(X_local.shape[0])*nugget_factor  # add a small nugget factor
        local_RKHS_norm_value = torch.sqrt(fX_local.reshape(1, -1) @ torch.inverse(K_local) @ fX_local.reshape(-1, 1)).flatten()  # inverse computation takes a lot of time but does not need to be done often. True RKHS norm given eigentlich once. And again also just for training
        while not local_RKHS_norm_value > 0:
            nugget_factor *= 10
            K_local = self.kernel(X_local, X_local).evaluate() + torch.eye(X_local.shape[0])*nugget_factor  # add a small nugget factor
            local_RKHS_norm_value = torch.sqrt(fX_local.reshape(1, -1) @ torch.inverse(K_local) @ fX_local.reshape(-1, 1)).flatten()
        return local_RKHS_norm_value


class safe_BO():
    def __init__(self, delta_confidence, delta_cube, noise_std, tuple_ik, X_plot, X_sample,
                Y_sample, safety_threshold, exploration_threshold, gt, compute_local_X_plot, compute_all_sets=False):
        def compute_X_plot_locally(n_dimensions, points_per_axis, lb, ub):  # for scalability; discretization within each sub-domain separately
            X_plot = []
            for i in range(n_dimensions):
                X_plot_per_domain = torch.linspace(lb[i], ub[i], points_per_axis)
                X_plot.append(X_plot_per_domain)
            X_plot = torch.cartesian_prod(*X_plot).reshape(-1, n_dimensions)
            return X_plot
        self.compute_all_sets = compute_all_sets
        self.gt = gt
        self.exploration_threshold = exploration_threshold
        self.delta_confidence = delta_confidence
        self.X_plot = X_plot
        self.noise_std = noise_std
        self.n_dimensions = X_plot.shape[1]
        self.best_lower_bound_local = -np.infty
        self.safety_threshold = safety_threshold
        self.tuple = tuple_ik
        self.lambda_bar = max(self.noise_std, 1)
        (i, k) = tuple_ik
        if (i, k) != (-1, -1):
            self.lb = X_sample[i]-delta_cube*(k+1)
            self.ub = X_sample[i]+delta_cube*(k+1)
            self.lb[self.lb < 0] = 0
            self.ub[self.ub > 1] = 1  # clipping
            sample_indices = torch.all(torch.logical_and(X_sample >= self.lb, X_sample <= self.ub), axis=1)
            self.x_sample = X_sample[sample_indices].clone().detach()
            self.y_sample = Y_sample[sample_indices].clone().detach()
            if not compute_local_X_plot:
                self.discr_domain = X_plot[torch.all(torch.logical_and(self.X_plot >= self.lb, self.X_plot <= self.ub), axis=1)]
            else:
                self.discr_domain = compute_X_plot_locally(n_dimensions=self.X_plot.shape[1], points_per_axis=int(np.round(self.X_plot.shape[0]**(1/self.X_plot.shape[1]))),
                                                           lb=self.lb, ub=self.ub)
        else:
            self.lb = torch.tensor([0]*X_plot.shape[1])
            self.ub = torch.tensor([1]*X_plot.shape[1])
            self.x_sample = X_sample
            self.y_sample = Y_sample
            self.discr_domain = X_plot

    def compute_model(self, dict_reuse_GPs, gpr):
        if convert_to_hashable(self.x_sample) in dict_reuse_GPs.keys():
            self.model, self.K = dict_reuse_GPs[convert_to_hashable(self.x_sample)]
        else:
            if Furuta:
                self.model = gpr(train_x=self.x_sample, train_y=self.y_sample, noise_std=self.noise_std, lengthscale=0.2)  # only change of the lengthscale
            else: 
                self.model = gpr(train_x=self.x_sample, train_y=self.y_sample, noise_std=self.noise_std, lengthscale=0.1)
            self.K = self.model(self.x_sample).covariance_matrix
            # model.train()
            dict_reuse_GPs[convert_to_hashable(self.x_sample)] = [self.model, self.K]
        # return model

    def compute_mean_var(self):  # GP model predictions
        self.model.eval()
        self.f_preds = self.model(self.discr_domain)
        self.mean = self.f_preds.mean
        self.var = self.f_preds.variance

    def compute_confidence_intervals_training(self, dict_local_RKHS_norms={}):
        if self.tuple in dict_local_RKHS_norms:
            self.B = dict_local_RKHS_norms[self.tuple]
        else:
            self.B = self.compute_RKHS_norm_true()
            dict_local_RKHS_norms[self.tuple] = self.B
        self.compute_beta()
        self.lcb = self.mean - self.beta*torch.sqrt(self.var)  # we have to use standard deviation instead of variance
        self.ucb = self.mean + self.beta*torch.sqrt(self.var)
        return dict_local_RKHS_norms

    def compute_confidence_intervals_evaluation(self, RNN_model=None, m_PAC=None, alpha_bar=None, PAC=False, RKHS_norm_guessed=None):  # PAC is a boolean that decides whether we are in the outer loop or inner loop
        if RKHS_norm_guessed is None:
            self.B = predict(RNN_model, self.RKHS_norm_mean_function_list, self.vi_frac_list)
            if PAC:
                list_random_RKHS_norms = []
                N_hat = int(max(torch.round((torch.max(self.ub-self.lb))*500), len(self.y_sample) + 10))
                print(f'Getting PAC bounds now for cube {self.tuple}.')
                x_interpol = self.x_sample
                y_interpol = self.y_sample
                for _ in range(m_PAC):
                    X_c = (torch.min(self.discr_domain) - torch.max(self.discr_domain))*torch.rand(N_hat, self.x_sample.shape[1]) + torch.max((self.discr_domain))
                    X_c_tail = X_c[x_interpol.shape[0]:]
                    X_c[:self.x_sample.shape[0]] = x_interpol
                    alpha_tail = -2*alpha_bar*torch.rand(N_hat-len(y_interpol), 1) + alpha_bar
                    y_tail = self.model.kernel(x_interpol, X_c_tail).evaluate() @ alpha_tail
                    y_head = (y_interpol - torch.squeeze(y_tail)).reshape(-1, 1)
                    # matrix inversion with nugget factor for regularization
                    # Following line satisfies interpolation propoert
                    alpha_head = torch.inverse(self.model.kernel(x_interpol, x_interpol).evaluate()+torch.eye(len(y_interpol))*1e-3) @ y_head
                    alpha = torch.cat((alpha_head, alpha_tail))
                    random_RKHS_norm = torch.sqrt(alpha.T @ self.model.kernel(X_c, X_c).evaluate() @ alpha)
                    list_random_RKHS_norms.append(random_RKHS_norm)
                numpy_list = [tensor.item() for tensor in list_random_RKHS_norms]
                numpy_list.sort()
                r_final = 0
                for r in range(m_PAC):
                    summ = 0
                    for i in range(r):
                        summ += comb(m_PAC, i)*gamma_PAC**(i)*(1-gamma_PAC)**(m_PAC-i)
                    if summ > kappa_PAC or self.B > numpy_list[-1-r]:
                        break
                    else:
                        r_final = r
                self.B = max(self.B, numpy_list[-1-r_final])  # Algorithm 3; scenario approach with PAC bounds
        elif RKHS_norm_guessed is not None:
            self.B = RKHS_norm_guessed
        self.compute_beta()
        self.lcb = self.mean - self.beta*torch.sqrt(self.var)  # we have to use standard deviation instead of variance
        self.ucb = self.mean + self.beta*torch.sqrt(self.var)

    def compute_safe_set(self):
        self.S = self.lcb > self.safety_threshold

        # Auxiliary objects of potential maximizers M and potential expanders G
        self.G = self.S.clone()
        self.M = self.S.clone()

    def maximizer_routine(self, best_lower_bound_others):
        self.M[:] = False  # initialize
        self.max_M_var = 0  # initialize
        if not torch.any(self.S):  # no safe points
            return
        self.best_lower_bound_local = max(self.lcb[self.S])
        self.M[self.S] = self.ucb[self.S] >= max(best_lower_bound_others, self.best_lower_bound_local)
        self.M[self.M.clone()] = (self.ucb[self.M] - self.lcb[self.M]) > self.exploration_threshold
        if not torch.any(self.M):
            return
        self.max_M_var = torch.max(self.ucb[self.M] - self.lcb[self.M])
        self.max_M_ucb = torch.max(self.ucb[self.M])

    def expander_routine(self):
        self.G[:] = False  # initialize
        if not torch.any(self.S) or torch.all(self.S):  # no safe points or all of them are safe points -> no possible expanders
            return
        # no need to consider points in M
        if self.compute_all_sets:  # for visualization; introductory example
            s = self.S.clone()
        else:
            s = torch.logical_and(self.S, ~self.M)
            s[s.clone()] = (self.ucb[s] - self.lcb[s]) > self.max_M_var
            s[s.clone()] = (self.ucb[s] - self.lcb[s]) > self.exploration_threshold  # only sufficiently uncertain.
        # still same size as the safe set! We are just over-writing the positive ones
        if not torch.any(s):
            return
        potential_expanders = self.discr_domain[s]
        unsafe_points = self.discr_domain[~self.S]
        kernel_distance = self.compute_kernel_distance(potential_expanders, unsafe_points)
        ucb_expanded = self.ucb[s].unsqueeze(1).expand(-1, kernel_distance.size(1))
        s[s.clone()] = torch.any(ucb_expanded - self.B*kernel_distance > self.safety_threshold, dim=1)
        # or go with for loop; might be more scalable, but slower for smaller dimensions
        # boolean_expander = ~s[s.clone()]  # assume that all are NOT expanders and go in the loop
        # for i in range(len(potential_expanders)):
        #     potential_expander = potential_expanders[i]
        #     for unsafe_point in unsafe_points:
        #         if self.ucb[s][i] - self.compute_kernel_distance(potential_expander, unsafe_point) > self.safety_threshold:
        #             boolean_expander[i] = True
        #             break  # we only need one!  
        # s[s.clone()] = boolean_expander  # update the potential expanders on whether they can potentially expand to an unsafe point            
        self.G = s

    def compute_beta(self):
        # Fiedler et al. 2024 Equation (7); based on Abbasi-Yadkori 2013
        inside_log = torch.det(torch.eye(self.x_sample.shape[0]) + (1/self.noise_std*self.K))
        inside_sqrt = self.noise_std*torch.log(inside_log) - (2*self.noise_std*torch.log(torch.tensor(self.delta_confidence)))
        self.beta = self.B + torch.sqrt(inside_sqrt)


    def compute_RKHS_norm_true(self):
        return self.gt.local_RKHS_norm(lb=self.lb, ub=self.ub, X_plot_local=self.discr_domain) if self.tuple != (-1,-1) else self.gt.RKHS_norm

    def compute_kernel_distance(self, x, x_prime):  # let us try whether it works without reshaped!
        '''
        k(x,x)+k(x^\prime,x^\prime)-k(x,x^\prime)-k(x^\prime,x)=2-2k(x,x^\prime)
        This holds for all radial kernels with output variance 1, i.e., k(x,x)\equiv 1.
        Both of which are true for our case.
        We have this setting and we exploit it.
        '''
        if self.model.kernel.__class__.__name__ != 'MaternKernel':  # This work only uses Matérn kernels
            raise Exception("Current implementation only works with radial kernels.")
        matrix_containing_kernel_values = self.model.kernel(x, x_prime).evaluate()  # here we can have problems with the size of the matrix
        return torch.sqrt(2-2*matrix_containing_kernel_values)

    def save_data_for_RNN_training(self, dict_mean_RKHS_norms, dict_recip_variances, x_last_iteration):
        if convert_to_hashable(self.tuple) not in dict_mean_RKHS_norms.keys():
            alpha = torch.inverse(self.K+self.noise_std**2*torch.eye(self.K.shape[0])) @ self.y_sample
            self.RKHS_norm_mean_function_list = [torch.sqrt(alpha.reshape(1, -1) @ self.K @ alpha.reshape(-1, 1)).flatten()]  # RKHS norm of the mean.
            dict_mean_RKHS_norms[self.tuple] = self.RKHS_norm_mean_function_list

            variance_integral = sum(self.var)/2*(self.n_dimensions/self.discr_domain.shape[0])
            self.vi_frac_list = [1/variance_integral]  # reciprocal covariance integral
            dict_recip_variances[self.tuple] = self.vi_frac_list
        elif x_last_iteration is None:
            pass
        elif torch.all(torch.logical_and(x_last_iteration >= self.lb, x_last_iteration <= self.ub)):
            alpha = torch.inverse(self.K+self.noise_std**2*torch.eye(self.K.shape[0])) @ self.y_sample
            RKHS_norm_mean_function = torch.sqrt(alpha.reshape(1, -1) @ self.K @ alpha.reshape(-1, 1)).flatten()
            dict_mean_RKHS_norms[self.tuple].append(RKHS_norm_mean_function)
            self.RKHS_norm_mean_function_list = dict_mean_RKHS_norms[self.tuple]

            variance_integral = sum(self.var)/2*(self.n_dimensions/self.discr_domain.shape[0])
            dict_recip_variances[self.tuple].append(1/variance_integral)
            self.vi_frac_list = dict_recip_variances[self.tuple]
        else:  # Already in the list and new point is not influencing the sub-domain
            self.vi_frac_list = dict_recip_variances[self.tuple]
            self.RKHS_norm_mean_function_list = dict_mean_RKHS_norms[self.tuple]
        return dict_mean_RKHS_norms, dict_recip_variances


def run(args):
    training = args[-1]
    if training:  # boolean whether we are training or not
        hyperparameters, num_iterations, X_plot, RKHS_norm = args[:-1]
        global_approach = False
        noise_std, delta_confidence, exploration_threshold, delta_cube, num_local_cubes, compute_local_X_plot = hyperparameters
    if not training:
        hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, RNN_model, compute_local_X_plot = args[:-1]
        run_type = hyperparameters[-1]
        if run_type == 'SafeOpt':  # to recreate SafeOpt. This uses no localization and keeps the a priori guess on the RKHS norm
            delta_cube = 1
            noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, _ = hyperparameters
        else:
            compute_all_sets = False
            noise_std, delta_confidence, alpha_bar, m_PAC, gamma_PAC, kappa_PAC,\
            exploration_threshold, delta_cube, num_local_cubes, _ = hyperparameters
    global_cube_list = []
    dict_mean_RKHS_norms = {}
    dict_recip_variances = {}
    if training:
        run_type = 'ours'
        dict_local_RKHS_norms = {}
        list_training = []  # we can do that a posteriori
        gt = ground_truth(num_center_points=np.random.choice(range(600, 1000)), X_plot=X_plot, RKHS_norm=RKHS_norm)
        X_sample_init, Y_sample_init = initial_safe_samples(gt=gt, num_safe_points=num_safe_points)
        X_sample = X_sample_init.clone()
        Y_sample = Y_sample_init.clone()
    if not global_approach:
        interesting_domains = set((i, k) for i in range(num_safe_points) for k in range(num_local_cubes))
        interesting_domains.add(tuple([-1, -1]))
    elif global_approach:
        interesting_domains={tuple([-1, -1])}

    x_new_last_iteration = torch.tensor([-torch.inf for _ in range(n_dimensions)])  # init
    best_lower_bound_others = -np.infty  # init
    skip_global_domain = False  # init
    while X_sample.shape[0] <= num_iterations:
        try:
            del chosen_cube  # just delete it completely
        except NameError:
            pass
        best_lower_bound_others = -np.infty
        max_uncertainty_interesting = 0  # max uncertainty of interesting domain
        dict_reuse_GPs = {}
        current_interesting_domains = interesting_domains.copy()
        if (-1, -1) in current_interesting_domains:  # we should iterate with the global domain
            current_interesting_domains.remove((-1, -1))
            domains_to_iterate_through = [(-1, -1), *current_interesting_domains]
        else:
            domains_to_iterate_through = [(-1, -1), *current_interesting_domains]  # global domain not interesting
        if skip_global_domain:
            domains_to_iterate_through.remove((-1, -1))
        if not training:
            print(f'We have {len(domains_to_iterate_through)} cubes to iterate through.')  # In SafeOpt, this is always 1
        for (i, k) in domains_to_iterate_through:  # start off with global domain; sensible heuristic
            skip_global_domain = False  # only valid when starting the while loop and we want to skip the global domain for next round.
            try:
                del cube  # reset
            except NameError:
                pass
            cube = safe_BO(delta_confidence=delta_confidence, delta_cube=delta_cube, noise_std=noise_std, tuple_ik=(i, k), X_plot=X_plot, X_sample=X_sample,
                            Y_sample=Y_sample, safety_threshold=gt.safety_threshold, exploration_threshold=exploration_threshold, gt=gt,
                            compute_local_X_plot=compute_local_X_plot, compute_all_sets=compute_all_sets)  # all samples that we currently have
            cube.compute_model(dict_reuse_GPs, gpr=GPRegressionModel)
            cube.compute_mean_var()
            if run_type == 'ours':
                dict_mean_RKHS_norms, dict_recip_variances = cube.save_data_for_RNN_training(dict_mean_RKHS_norms, dict_recip_variances, x_new_last_iteration)  # RKHS norm and reciprocal covariance integral
            if training:
                dict_local_RKHS_norms = cube.compute_confidence_intervals_training(dict_local_RKHS_norms=dict_local_RKHS_norms)
            else:
                if run_type == 'ours':
                    cube.compute_confidence_intervals_evaluation(RNN_model=RNN_model, m_PAC=m_PAC, alpha_bar=alpha_bar, PAC=False)  # We do not need PAC bounds yet; improves speed. PAC bounds essential for sampling
                elif run_type == 'SafeOpt':
                    cube.compute_confidence_intervals_evaluation(RKHS_norm_guessed=B)
            cube.compute_safe_set()
            cube.maximizer_routine(best_lower_bound_others=best_lower_bound_others)
            cube.expander_routine()
            if cube.best_lower_bound_local > best_lower_bound_others:
                best_lower_bound_others = cube.best_lower_bound_local
            if not torch.any(torch.logical_or(cube.M, cube.G)):
                if cube.tuple in interesting_domains:
                    interesting_domains.remove(cube.tuple)
            else:
                max_uncertainty_interesting_local = max((cube.ucb - cube.lcb)[torch.logical_or(cube.M, cube.G)])
                x_new_current = cube.discr_domain[torch.logical_or(cube.M, cube.G)][torch.argmax(cube.var[torch.logical_or(cube.M, cube.G)])]
                if not torch.any(torch.all(X_sample == x_new_current, axis=1)) and max_uncertainty_interesting_local > max_uncertainty_interesting:
                    max_uncertainty_interesting = max_uncertainty_interesting_local
                    chosen_tuple = cube.tuple
                    chosen_cube = cube
                    x_new = x_new_current
                elif torch.any(torch.all(X_sample == x_new_current, axis=1)) and cube.tuple in interesting_domains:
                    interesting_domains.remove(cube.tuple)
        if run_type == 'SafeOpt':
            global_cube_list.append(cube)
        if not training and run_type == 'ours':
            # Now with PAC bounds!
            try:  # there is a chosen cube
                auxx = chosen_cube.tuple  # auxiliary action
            except:  # there is no chosen cube
                if not training:
                    print('Our algorithm terminated! There is no input that we can/want to sample next.')
                break

            dict_local_RKHS_norms = chosen_cube.compute_confidence_intervals_evaluation(RNN_model, m_PAC, alpha_bar, PAC=True)
            chosen_cube.compute_safe_set()
            chosen_cube.maximizer_routine(best_lower_bound_others=best_lower_bound_others)
            chosen_cube.expander_routine()
            if torch.any(torch.logical_or(chosen_cube.M, chosen_cube.G)):
                x_new_current = chosen_cube.discr_domain[torch.logical_or(chosen_cube.M, chosen_cube.G)][torch.argmax(chosen_cube.var[torch.logical_or(chosen_cube.M, chosen_cube.G)])]
                if not torch.any(torch.all(X_sample == x_new_current, axis=1)):
                    x_new = x_new_current
                    print(f'The chosen cube is {chosen_tuple} and the input is {x_new}, with PAC RKHS norm {chosen_cube.B}.')
            if not torch.any(torch.logical_or(chosen_cube.M, chosen_cube.G)) or torch.any(torch.all(X_sample == x_new_current, axis=1)):
                if len(interesting_domains) == 0:
                    if not training:
                        print('Our algorithm terminated! There is no input that we can/want to sample next.')
                    break
                if chosen_cube.tuple in interesting_domains:
                    interesting_domains.remove(chosen_cube.tuple)
                    print('Skipping this domain after PAC check')
                    if chosen_cube.tuple == (-1, -1):
                        skip_global_domain = True
                    x_new_last_iteration = None if torch.any(x_new_last_iteration > np.infty) else x_new_last_iteration
                    continue
        else:
            try:
                # Auxiliary action but no print
                aux = copy.deepcopy(x_new)
            except:
                print(f'{run_type} terminated! There is no input that we can/want to sample next.')
                break
        if not training or training:
            pass
        y_new = gt.conduct_experiment(x=x_new, noise_std=noise_std)
        if y_new < gt.safety_threshold:
            if training or run_type == 'SafeOpt':
                warnings.warn('Sampled unsafe point!')  # this can happen with under-estimates RKHS norms
            else:  # we do not tolerate unsafe sampling
                raise Exception('Sampled unsafe point!')
        X_sample = torch.cat((X_sample, x_new.unsqueeze(0)), dim=0)
        Y_sample = torch.cat((Y_sample, y_new), dim=0)
        if Furuta:  # always save in hardware;
            with open('furuta_hardware_X_sample.pickle', 'wb') as handle:
                pickle.dump(X_sample, handle, protocol=pickle.HIGHEST_PROTOCOL)
            with open('furuta_hardware_Y_sample.pickle', 'wb') as handle:
                pickle.dump(Y_sample, handle, protocol=pickle.HIGHEST_PROTOCOL)
            print(f'Saved data. We currently gathered {len(Y_sample)} samples.')




        # Which sub-domain changed through this new sample?
        if not global_approach:
            X_distance = torch.max(torch.abs(X_sample - x_new), dim=1).values
            effect_tensor = X_distance.unsqueeze(1) <= torch.arange(1, num_local_cubes + 1) * delta_cube
            indices = torch.nonzero(effect_tensor, as_tuple=False)
            indices_set = {(i.item(), k.item()) for i, k in indices}
            interesting_domains |= indices_set  # set union
            interesting_domains.add((-1, -1))
        x_new_last_iteration = copy.deepcopy(x_new)
        del x_new  # There is no x_new for the next iteration
    if training:
        list_training = []
        for key in dict_mean_RKHS_norms.keys():
            list_training.append([dict_mean_RKHS_norms[key], dict_recip_variances[key], dict_local_RKHS_norms[key]])
        return list_training  # all values that we got/need from ONE random RKHS function for training
    if not training:
        return X_sample, Y_sample, global_cube_list, gt.safety_threshold


if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # Hyperparameters
    Furuta = False  # set to True to conduct policy parameter optimization on the Furuta pendulum
    training = False  # set true to create training data for the RNN
    Gym = False  # set to True to conduct OpenAI Gym experiments
    if training and Gym:
        raise Exception('Cannot get training runs with Gym')
    noise_std = 0.01  # standard deviation of noise, and in GPs
    delta_confidence = 0.01  # yields 99% confidence for safety proof.
    num_safe_points = 1  # singleton safe set
    num_iterations = 30  # iterations. We used 50 iterations to get training data and for the Gym experiments, 30 otherwise
    exploration_threshold = 0.1  # exploration threshold, see Sui et al. 2015
    n_dimensions = 1
    points_per_axis = 1000  # 30 for 4D, 1000 for 1D, 500 for 2D, 100 for 3D, 8 for 6D. Depends on computational resources, also a "hyperparameter"

    # Initialize our algorithm
    X_plot = compute_X_plot(n_dimensions, points_per_axis)
    delta_cube = 0.1  # hyperparameter
    num_local_cubes = 5

    introductory_example = False  # Fig 1 if True, other numerical experiments if False
    if introductory_example and Gym:
        raise Exception("Cannot do both at once.")
    if introductory_example:
        compute_all_sets = True  # this is only if we want to plot the sets, Figure 1 of paper.
    else:
        compute_all_sets = False
    reproduce_experiments = True  # set to True if you want to reproduce the experiments of either introductory or numerical example

    # For training
    if training:
        compute_local_X_plot = False
        hyperparameters = [noise_std, delta_confidence, exploration_threshold, delta_cube, num_local_cubes, compute_local_X_plot]
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            parallel = False 
            number_of_random_RKHS_function = 1000
            task_input = [(hyperparameters, num_iterations, X_plot, np.random.uniform(0.5, 30), training) for _ in range(number_of_random_RKHS_function)]
            if parallel:
                with mp.Pool() as pool:
                    collected_training = pool.map(run, task_input)
            else:
                collected_list_training = []
                for task in tqdm(task_input):
                    list_training = run(task)
                    collected_list_training.append(list_training)
        with open('1D_training_data.pickle', 'wb') as handle:
            pickle.dump(collected_list_training, handle, protocol=pickle.HIGHEST_PROTOCOL)
        print('Training finished!')
    # For "evaluating"
    if not training and not Gym and not Furuta:
        kappa_PAC = 0.01  # confidence PAC bounds
        gamma_PAC = 0.1  # probability PAC bounds
        m_PAC = 1000  # number of random RKHS function created for PAC bounds
        alpha_bar = 1
        RKHS_norm = 5  # np.random.uniform(0.5, 30)
        gt = ground_truth(num_center_points=1000, X_plot=X_plot, RKHS_norm=RKHS_norm)
        X_sample_init, Y_sample_init = initial_safe_samples(gt=gt, num_safe_points=num_safe_points)
        X_sample = X_sample_init.clone()
        Y_sample = Y_sample_init.clone()
        if reproduce_experiments and not Gym:  # NOTE: experiments are still random, since sampling is noisy
            if introductory_example:
                noise_std = 0.05
                with open('Experiments/SafeOpt_RKHS_intro/gt.pickle', 'rb') as handle:
                    gt = dill.load(handle)
                with open('Experiments/SafeOpt_RKHS_intro/X_sample.pickle', 'rb') as handle:
                    X_sample = dill.load(handle)
                with open('Experiments/SafeOpt_RKHS_intro/Y_sample.pickle', 'rb') as handle:
                    Y_sample = dill.load(handle)
            elif not Gym and n_dimensions == 1:
                noise_std = 0.01
                with open('Github/gt.pickle', 'rb') as handle:
                    gt = dill.load(handle)
                with open('Github/X_sample.pickle', 'rb') as handle:
                    X_sample = dill.load(handle)
                with open('Github/Y_sample.pickle', 'rb') as handle:
                    Y_sample = dill.load(handle)
            elif not Gym and n_dimensions == 2:
                with open('Experiments/2D_toy_experiments/gt.pickle', 'rb') as handle:
                    gt = dill.load(handle)
                with open('Experiments/2D_toy_experiments/X_sample.pickle', 'rb') as handle:
                    X_sample = dill.load(handle)
                with open('Experiments/2D_toy_experiments/Y_sample.pickle', 'rb') as handle:
                    Y_sample = dill.load(handle)
        if introductory_example:
            with torch.no_grad(), gpytorch.settings.fast_pred_var():
                if reproduce_experiments:
                    RKHS_norm = gt.RKHS_norm
                compute_local_X_plot = False
                run_type = 'SafeOpt'
                global_approach = True

                # Plot reconstruction for introductory experiment. Use following arguments in the plot function
                # "global_cube_list_under[0]" for initial
                # "global_cube_list_under[10]" to get plot after 10 iterations
                # "global_cube_list_under[-1]" to get plot after 30 iterations

                B = RKHS_norm/5
                hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
                X_sample_SO_under, Y_sample_SO_under, global_cube_list_under, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
                plot_1D_SafeOpt_with_sets(global_cube_list_under[0], gt, save=False, title='SafeOpt under first')

                B = RKHS_norm
                hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
                X_sample_SO_true, Y_sample_SO_true, global_cube_list_true, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
                plot_1D_SafeOpt_with_sets(global_cube_list_true[-1], gt, save=False, title='SafeOpt true last')

                B = RKHS_norm*5
                hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
                X_sample_SO_over, Y_sample_SO_over, global_cube_list_over, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
                plot_1D_SafeOpt_with_sets(global_cube_list_over[0], gt, save=False, title='SafeOpt over first')

        if not introductory_example:
            with torch.no_grad(), gpytorch.settings.fast_pred_var():
                if reproduce_experiments:
                    RKHS_norm = gt.RKHS_norm
                compute_local_X_plot = False
                run_type = 'SafeOpt'
                global_approach = True
                B = RKHS_norm/5
                hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
                X_sample_SO_under, Y_sample_SO_under, global_cube_list_under, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
                if n_dimensions == 1:
                    plot_1D(X_sample_SO_under, Y_sample_SO_under, X_plot, gt.fX, title='SafeOpt under', safety_threshold=gt.safety_threshold, save=False)
                elif n_dimensions == 2:
                    plot_2D_contour(X_plot, gt.fX, X_sample_SO_under, Y_sample=Y_sample_SO_under, safety_threshold=gt.safety_threshold, title='SafeOpt under', levels=10, save=False) 

                B = RKHS_norm*5
                hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
                X_sample_SO_over, Y_sample_SO_over, global_cube_list_over, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
                if n_dimensions == 1:
                    plot_1D(X_sample_SO_over, Y_sample_SO_over, X_plot, gt.fX, title='SafeOpt over', safety_threshold=gt.safety_threshold, save=False)
                elif n_dimensions == 2:
                    plot_2D_contour(X_plot, gt.fX, X_sample_SO_over, Y_sample=Y_sample_SO_over, safety_threshold=gt.safety_threshold, title='SafeOpt under', levels=10, save=False) 

                compute_local_X_plot = True
                run_type = 'ours'
                hyperparameters = [noise_std, delta_confidence, alpha_bar, m_PAC, gamma_PAC, kappa_PAC,
                                    exploration_threshold, delta_cube, num_local_cubes, run_type]
                model_path = "rnn_model.pt"
                hidden_size = 20
                num_layers = 2
                num_classes = 1
                RNN_model = load_model(model_path, hidden_size, num_layers, num_classes)
                global_approach = False
                X_sample_our, Y_sample_our, global_cube, safety_threshold = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, RNN_model, compute_local_X_plot, training])
                if n_dimensions == 1:
                    plot_1D(X_sample=X_sample_our, Y_sample=Y_sample_our, X_plot=X_plot, fX=gt.fX, title='ours', safety_threshold=safety_threshold, save=False)
                elif n_dimensions == 2:
                    plot_2D_contour(X_plot, gt.fX, X_sample_our, Y_sample=Y_sample_our, safety_threshold=gt.safety_threshold, title='ours', levels=10, save=False)

    if Gym:

        kappa_PAC = 0.01  # confidence PAC bounds
        gamma_PAC = 0.1  # probability PAC bounds
        m_PAC = 1000  # number of random RKHS function created for PAC bounds
        alpha_bar = 1
        # environment = "InvertedPendulumSwingupBulletEnv-v0"
        environment = "LunarLanderContinuous-v2"
        # environment = "ReacherBulletEnv-v0"
        # environment = "Swimmer-v4"
        # environment = "MountainCarContinuous-v0"
        # environment = "Hopper-v5"
        # environment = "HalfCheetah-v5"
        # environment = "BipedalWalker-v3"
        noise_std = 0.01  # standard deviation
        delta_confidence = 0.01  # yields 99% confidence for safety proof.
        num_safe_points = 1  # singleton safe set
        num_iterations = 50  # number of iterations
        exploration_threshold = 0.1  # exploration threshold, see Sui et al. 2015
        n_dimensions = 2  # depends on experiment
        points_per_axis = 500  # 30 for 4D, 1000 for 1D, 400-500 for 2D, 100 for 3D, 15 for 5D
        X_plot = compute_X_plot(n_dimensions, points_per_axis)
        gte = ground_truth_experiment(environment=environment)
        X_sample_init, Y_sample_init = gte.initial_safe_samples()
        X_sample = X_sample_init.clone().to(torch.float32)
        Y_sample = Y_sample_init.clone()
        print(X_sample, Y_sample)
        print(X_sample.dtype, Y_sample.dtype)
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            compute_local_X_plot = False
            run_type = 'SafeOpt'

            # Conservative RKHS norm
            global_approach = True
            B = 30
            hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, compute_all_sets, run_type]
            print(run_type, B)
            X_sample_SO_over, Y_sample_SO_over, _, best_lower_bound_SO_over, best_lower_bound_x_SO_over, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gte, X_plot, global_approach, None, compute_local_X_plot, training])

            # Not so conservative guess
            B = 0.2
            print(run_type, B)
            hyperparameters = [noise_std, delta_confidence, exploration_threshold, B, run_type]
            X_sample_SO_under, Y_sample_SO_under, global_cube_list_under, _ = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, None, compute_local_X_plot, training])
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            compute_local_X_plot = True
            run_type = 'ours'
            print(run_type)
            hyperparameters = [noise_std, delta_confidence, alpha_bar, m_PAC, gamma_PAC, kappa_PAC,
                                exploration_threshold, delta_cube, num_local_cubes, run_type]
            RNN_model_path = "rnn_model.pt"
            model_path = "rnn_model.pt"
            hidden_size = 20
            num_layers = 2
            num_classes = 1
            RNN_model = load_model(model_path, hidden_size, num_layers, num_classes)
            global_approach = False
            X_sample_our, Y_sample_our, global_cube, safety_threshold = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt, X_plot, global_approach, RNN_model, compute_local_X_plot, training])
            print("Finished; starting plots.")
            plot_gym(Y_sample=Y_sample_SO_over, safety_threshold=0, title='SafeOpt over', save=False)
            plot_gym(Y_sample=Y_sample_SO_under, safety_threshold=0, title='SafeOpt under', save=False)
            plot_gym(Y_sample=Y_sample_our, safety_threshold=0, title='ours', save=False)
            plot_gym_together(Y_sample_SO_under, Y_sample_SO_over, Y_sample_our, safety_threshold=0, title='experiment-together', save=False)
    
    if Furuta:
        class ground_truth_Furuta():
            def __init__(self, safety_threshold, use_simulator):
                self.use_simulator = use_simulator
                self.safety_threshold = safety_threshold
                self.frequency = 200
                self.freq_div = 4
                self.k_scale = np.diag([-10, 100, 1, 1])  # scale it a priori.
                self.last_two_entries = np.array([-1.5040040945983464, 3.0344775662414483])  # these are kept constant
                with QubeBalanceEnv(use_simulator=use_simulator, frequency=self.frequency) as env:
                    self.state_init = env.reset()

            def conduct_experiment(self, x, noise_std=None):
                param = np.asarray(x, dtype=np.float64)
                param = np.concatenate((param, self.last_two_entries))
                self.state = copy.deepcopy(self.state_init)
                reward = 0
                if not self.use_simulator:
                    IPS()
                with QubeSwingupEnv(use_simulator=self.use_simulator, frequency=self.frequency) as env:
                    env.reset()
                    swing_up_ctrl = QubeFlipUpControl(sample_freq=self.frequency, env=env)
                    upright = False
                    i = 0
                    while i < 1000:
                        if upright:
                            if np.abs(self.state[1]) < math.pi/2 and i % self.freq_div == 0:
                                action = np.dot(np.dot(self.k_scale, param.flatten()), self.state)
                            elif np.abs(self.state[1]) >= math.pi/2:
                                action = np.array([0.0])
                                print("failed during regular SafeOpt")
                            self.state, rew, _, _ = env.step(action.flatten())
                            reward += rew
                            i += 1
                        else:
                            action = swing_up_ctrl.action(self.state)*1.4
                            self.state, _, _, _ = env.step(action)
                            if not self.use_simulator:
                                print(np.linalg.norm(self.state))
                            if np.linalg.norm(self.state) < 5e-2:
                                print("swingup completed")
                                upright = True
                    print(f'Experiment done. For the parameter {x}, we received the reward {(reward/1000)}.')
                return torch.tensor([reward/1000], dtype=torch.float32)

            def try_furuta_real(self, param):
                param = np.asarray(param, dtype=np.float64).flatten()
                last_two_entries = self.last_two_entries
                param = np.concatenate((param, last_two_entries))
                use_simulator = self.use_simulator
                frequency = 200
                divider = 4
                constr = np.inf
                with QubeSwingupEnv(use_simulator=use_simulator, frequency=frequency) as env:
                    state = env.reset()
                    swing_up_ctrl = QubeFlipUpControl(sample_freq=frequency, env=env)
                    upright = False
                    i = 0
                    reward = 0
                    reward_LQR = 0
                    R = 1
                    Q = np.diag([5, 1, 1, 1])
                    while i < 1000:
                        if upright:
                            if np.abs(state[1]) < math.pi/2 and i % divider == 0:
                                action = np.dot(np.dot(self.k_scale, param), state)  # directly scale
                            elif np.abs(state[1]) >= math.pi/2:
                                action = np.array([0.0])
                                raise Exception("failed")
                            state, rew, _, _ = env.step(action.flatten())
                            reward += rew
                            reward_LQR += -(state.T@Q@state + (action**2*R))
                            dist_constr = [np.pi/2 - np.abs(state[idx]) for idx in range(2)]
                            if np.min(dist_constr) < constr:
                                constr = np.min(dist_constr)
                            i += 1
                        else:
                            action = swing_up_ctrl.action(state)*1.4
                            state, _, _, _ = env.step(action)
                            print(np.linalg.norm(state))
                            if np.linalg.norm(state) < 5e-2:
                                upright = True
                return torch.tensor([reward/1000], dtype=torch.float32)

        # Initializing hyperparameters
        noise_std = 0.01  # standard deviation
        delta_confidence = 0.01  # yields 99% confidence for safety proof.
        num_safe_points = 1  # singleton safe set
        num_iterations = 30  # number of iterations
        exploration_threshold = 0.1  # exploration threshold, see Sui et al. 2015
        n_dimensions = 2
        points_per_axis = 100  # 30 for 4D, 1000 for 1D, 400-500 for 2D, 100 for 3D, 15 for 5D
        X_plot = compute_X_plot(n_dimensions, points_per_axis)
        delta_cube = 0.075  # We can make it larger if we have too little exploration. Currently, only sampling around (0, 0)
        num_local_cubes = 3
        use_simulator = False
        gt_furuta = ground_truth_Furuta(safety_threshold=0.3, use_simulator=use_simulator)  # set use_simulator=False to use the hardware!
        # Initial Furuta ground truth object
        X_sample = torch.tensor([[0.2394, 0.4242]])  # initial safe policy
        Y_sample = gt_furuta.try_furuta_real(param=X_sample)
        compute_local_X_plot = True
        run_type = 'ours'
        kappa_PAC = 0.01  # confidence PAC bounds
        gamma_PAC = 0.1  # probability PAC bounds
        m_PAC = 1000  # number of random RKHS function created for PAC bounds
        alpha_bar = 1
        hyperparameters = [noise_std, delta_confidence, alpha_bar, m_PAC, gamma_PAC, kappa_PAC,
                           exploration_threshold, delta_cube, num_local_cubes, run_type]
        model_path = "rnn_model.pt"
        hidden_size = 20
        num_layers = 2
        num_classes = 1
        RNN_model = load_model(model_path, hidden_size, num_layers, num_classes)
        global_approach = False
        X_sample_our, Y_sample_our, global_cube, safety_threshold = run(args=[hyperparameters, num_iterations, X_sample, Y_sample, gt_furuta, X_plot, global_approach, RNN_model, compute_local_X_plot, training])        
        with open('furuta_hardware_X_sample.pickle', 'wb') as handle:
            pickle.dump(X_sample_our, handle, protocol=pickle.HIGHEST_PROTOCOL)
        with open('furuta_hardware_Y_sample.pickle', 'wb') as handle:
            pickle.dump(Y_sample_our, handle, protocol=pickle.HIGHEST_PROTOCOL)
        print('Done optimizing. Conducting a final experiment with the best policy parameter now.')
        if not use_simulator:
            IPS()
        best_index = torch.argmax(Y_sample_our)
        X_sample_best = X_sample_our[best_index, :]
        Y_sample_best = gt_furuta.try_furuta_real(param=X_sample_best)
        plt.figure()
        colors = -(1/torch.log(Y_sample))*2
        scatter = plt.scatter(X_sample[:, 0], X_sample[:, 1], s=100, c=colors, cmap='Blues')
        cbar = plt.colorbar(scatter)
        plt.xlabel('a1')
        plt.ylabel('a2')
        cbar.ax.set_yticks([min(colors), max(colors)])
        cbar.ax.set_yticklabels(['Low', 'High'])
