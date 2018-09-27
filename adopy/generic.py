from __future__ import absolute_import, division, print_function

import numpy as np
from scipy.stats import norm, multivariate_normal as mvnm
from scipy.special import logsumexp

from adopy.functions import expand_multiple_dims, get_nearest_grid_index, get_random_design_index, make_grid_matrix, \
    marginalize


class ADOGeneric(object):
    """Base class for ADOpy classes.

    Examples
    --------

    .. code-block:: python3
        :linenos:

        ado = ADOBase()
        for _ in range(num_trials):  # Loop for trials
            design = ado.get_design()
            response = get_response(design)
            ado.update(design, response)

    get_response functions is a pseudo-function to run an experiment and get
    a response.
    """

    def __init__(self):
        super(ADOGeneric, self).__init__()

        self.designs = []
        self.params = []

        self.label_design = []
        self.label_param = []

        self.cond_param = {}

        self.grid_design = None
        self.grid_param = None
        self.grid_response = None

        self.dg_memory = []  # [(design, response), ...]
        self.dg_grid_params = []
        self.dg_means = []
        self.dg_covs = []
        self.dg_priors = []
        self.dg_posts = []

        self.p_obs = None
        self.log_lik = None
        self.log_prior = None
        self.log_post = None
        self.marg_log_lik = None

        self.ent_obs = None
        self.ent_marg = None
        self.ent_cond = None
        self.mutual_info = None

        self.idx_opt = None
        self.design_opt = None

        self.flag_update_mutual_info = True

    ##################################################################################################################
    # Properties
    ##################################################################################################################

    @property
    def num_designs(self):
        """Number of design grid axes"""
        return self.grid_design.shape[-1]

    @property
    def num_params(self):
        """Number of parameter grid axes"""
        return self.grid_param.shape[-1]

    @property
    def prior(self):
        """Prior distributions of joint parameter space"""
        return np.exp(self.log_prior)

    @property
    def post(self):
        """Posterior distributions of joint parameter space"""
        return np.exp(self.log_post)

    @property
    def marg_post(self):
        """Marginal posterior distributions for each parameter"""
        return [marginalize(self.post, self.grid_param, i) for i in range(self.num_params)]

    @property
    def post_mean(self):
        """Estimated posterior means for each parameter"""
        return np.dot(self.post, self.grid_param)

    @property
    def post_cov(self):
        m = self.post_mean
        d = self.grid_param - m
        return np.dot(d.T, d * self.post.reshape(-1, 1))

    @property
    def post_sd(self):
        return np.sqrt(np.diag(self.post_cov))

    ##################################################################################################################
    # Methods
    ##################################################################################################################

    def initialize(self):
        self.p_obs = self._compute_p_obs()
        self.log_lik = ll = self._compute_log_lik()
        self.ent_obs = -np.multiply(np.exp(ll), ll).sum(-1)

        lp = np.ones(self.grid_param.shape[0])
        self.log_prior = lp - logsumexp(lp)
        self.log_post = self.log_prior.copy()

        if len(self.dg_grid_params) == 0:
            self.dg_grid_params.append(self.grid_param)

    @classmethod
    def compute_p_obs(cls):
        """Compute the probability of an observed response."""
        raise NotImplementedError('The classmethod compute_p_obs should be implemented.')

    def _compute_p_obs(self):
        """Compute the probability of getting observed response."""
        raise NotImplementedError('The method _compute_p_obs should be implemented.')

    def _compute_log_lik(self):
        """Compute the log likelihood."""
        raise NotImplementedError('The method _compute_log_lik should be implemented.')

    def _update_mutual_info(self):
        """Update mutual information using posterior distributions.

        If there is no nedd to update mutual information, it ends.
        The flag to update mutual information must be true only when
        posteriors are updated in :code:`update(design, response)` method.
        """
        # If there is no need to update mutual information, it ends.
        if not self.flag_update_mutual_info:
            return

        # Calculate the marginal log likelihood.
        lp = expand_multiple_dims(self.log_post, 1, 1)
        mll = logsumexp(self.log_lik + lp, axis=1)
        self.marg_log_lik = mll  # shape (num_design, num_response)

        # Calculate the marginal entropy and conditional entropy.
        self.ent_marg = -np.sum(np.exp(mll) * mll, -1)  # shape (num_designs,)
        self.ent_cond = np.sum(self.post * self.ent_obs, axis=1)  # shape (num_designs,)

        # Calculate the mutual information.
        self.mutual_info = self.ent_marg - self.ent_cond  # shape (num_designs,)

        # Flag that there is no need to update mutual information again.
        self.flag_update_mutual_info = False

    def get_design(self, kind='optimal'):
        r"""Choose a design with a given type.

        1. :code:`optimal`: an optimal design :math:`d^*` that maximizes the mutual information.

            .. math::
                \begin{align*}
                    p(y | d) &= \sum_\theta p(y | \theta, d) p_t(\theta) \\
                    I(Y(d); \Theta) &= H(Y(d)) - H(Y(d) | \Theta) \\
                    d^* &= \operatorname*{argmax}_d I(Y(d); |Theta) \\
                \end{align*}

        2. :code:`random`: a design randomly chosen.

        Parameters
        ----------
        kind : {'optimal', 'random'}, optional
            Type of a design to choose

        Returns
        -------
        design : array_like
            A chosen design vector
        """
        assert kind in {'optimal', 'random'}

        def get_design_optimal():
            return self.grid_design[np.argmax(self.mutual_info)]

        def get_design_random():
            return self.grid_design[get_random_design_index(self.grid_design)]

        if kind == 'optimal':
            self._update_mutual_info()
            return get_design_optimal()
        elif kind == 'random':
            return get_design_random()
        else:
            raise RuntimeError('An invalid kind of design: "{}".'.format(type))

    def update(self, design, response, store=True):
        r"""
        Update the posterior :math:`p(\theta | y_\text{obs}(t), d^*)` for
        all discretized values of :math:`\theta`.

        .. math::
            p(\theta | y_\text{obs}(t), d^*) =
                \frac{ p( y_\text{obs}(t) | \theta, d^*) p_t(\theta) }
                    { p( y_\text{obs}(t) | d^* ) }

        Parameters
        ----------
        design
            Design vector for given response
        response
            Any kinds of observed response
        store : bool
            Whether to store observations of (design, response).
        """
        if store:
            self.dg_memory.append((design, response))

        idx_design = get_nearest_grid_index(design, self.grid_design)
        idx_response = get_nearest_grid_index(np.array(response), self.grid_response)  # yapf: disable

        self.log_post += self.log_lik[idx_design, :, idx_response].flatten()
        self.log_post -= logsumexp(self.log_post)

        self.flag_update_mutual_info = True

    def update_grid(self, grid, rotation='eig', grid_type='q', prior='normal', append=False):
        """Update the grid space for model parameters (Dynamic Gridding method)."""
        assert rotation in {'eig', 'svd', 'none', None}
        assert grid_type in {'q', 'z'}
        assert prior in {'recalc', 'normal', None}
        assert self.cond_param is None or \
               (isinstance(self.cond_param, dict) and
                all([k in self.label_param for k in self.cond_param.keys()]) and
                all([hasattr(v, '__call__') for v in self.cond_param.values()]))

        m = self.post_mean
        cov = self.post_cov
        sd = self.post_sd

        if np.linalg.det(cov) == 0:
            print('Cannot update grid no more.')
            return

        # Calculate a rotation matrix
        r_inv = None
        if rotation == 'eig':
            el, ev = np.linalg.eig(cov)
            r_inv = np.dot(np.sqrt(np.diag(el)), np.linalg.inv(ev))
        elif rotation == 'svd':
            _, sg, sv = np.linalg.svd(cov)
            r_inv = np.dot(np.sqrt(np.diag(sg)), sv)
        elif rotation == 'none' or rotation is None:
            r_inv = np.diag(sd)

        # Find grid points from the rotated space
        g_axes = None
        if grid_type == 'q':
            assert all([0 <= v <= 1 for v in grid])
            g_axes = np.repeat(norm.ppf(np.array(grid)).reshape(-1, 1), self.num_params, axis=1)
        elif grid_type == 'z':
            g_axes = np.repeat(np.array(grid).reshape(-1, 1), self.num_params, axis=1)

        # Compute new grid on the initial space.
        g_star = make_grid_matrix(*[v for v in g_axes.T])
        grid_new = np.dot(g_star, r_inv) + m

        # Remove improper points not in the parameter space
        for k, f in self.cond_param.items():
            idx = self.label_param.index(k)
            grid_new = grid_new[list(map(f, grid_new[:, idx]))]

        self.dg_means.append(m)
        self.dg_covs.append(cov)
        self.dg_grid_params.append(grid_new)
        if append:
            self.grid_param = np.concatenate([self.grid_param, grid_new])
        else:
            self.grid_param = grid_new

        self.dg_priors.append(self.prior)
        self.dg_posts.append(self.post)
        if append:
            log_post_prev = self.log_post.copy()

        self.initialize()

        # Assign priors on new grid
        if prior == 'recalc':
            for d, y in self.dg_memory:
                self.update(d, y, False)
        elif prior == 'normal':
            if append:
                mvnm_prior = mvnm.pdf(grid_new, mean=m, cov=cov)
                self.log_prior = np.concatenate([log_post_prev, mvnm_prior])
                self.log_post = self.log_prior.copy()
            else:
                self.log_prior = mvnm.pdf(grid_new, mean=m, cov=cov)
                self.log_post = self.log_prior.copy()
        elif prior is None:
            pass