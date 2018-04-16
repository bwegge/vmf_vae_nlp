import numpy as np
import torch
from scipy import special as sp

from NVLL.util.util import GVar


class vMF(torch.nn.Module):
    def __init__(self, hid_dim, lat_dim, kappa=1):
        super().__init__()
        self.hid_dim = hid_dim
        self.lat_dim = lat_dim
        self.kappa = kappa
        # self.func_kappa = torch.nn.Linear(hid_dim, lat_dim)
        self.func_mu = torch.nn.Linear(hid_dim, lat_dim)
        self.kld = GVar(torch.from_numpy(vMF._vmf_kld(kappa, lat_dim)).float())

    def estimate_param(self, latent_code):
        # kappa = self.func_kappa(latent_code)
        kappa = self.kappa
        mu = self.func_mu(latent_code)
        mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)  # TODO
        return {'mu': mu, 'kappa': kappa}

    def compute_KLD(self, tup):
        return self.kld

    @staticmethod
    def _vmf_kld(k, d):
        tmp = (k * ((sp.iv(d / 2.0 + 1.0, k) + sp.iv(d / 2.0, k) * d / (2.0 * k)) / sp.iv(d / 2.0, k) - d / (2.0 * k)) \
               + d * np.log(k) / 2.0 - np.log(sp.iv(d / 2.0, k)) \
               - sp.loggamma(d / 2 + 1) - d * np.log(2) / 2).real
        return np.array([tmp])

    def build_bow_rep(self, lat_code, n_sample):
        batch_sz = lat_code.size()[0]
        tup = self.estimate_param(latent_code=lat_code)
        mu = tup['mu']
        kappa = tup['kappa']

        kld = self.compute_KLD(tup)
        vecs = []
        if n_sample == 1:
            return tup, kld, self.sample_cell(mu, kappa)
        for n in range(n_sample):
            sample = self.sample_cell(mu, kappa)
            vecs.append(sample)
        return tup, kld, vecs

    def sample_cell(self, mu, kappa):
        batch_sz, lat_dim = mu.size()
        result = []
        sampled_vecs = GVar(torch.FloatTensor(batch_sz, lat_dim))
        for b in range(batch_sz):
            this_mu = mu[b]
            # kappa = np.linalg.norm(this_theta)
            this_mu = this_mu / torch.norm(this_mu, p=2)

            w = self._sample_weight(kappa, lat_dim)
            w_var = GVar(w * torch.ones(lat_dim))

            v = self._sample_orthonormal_to(this_mu, lat_dim)

            scale_factr = torch.sqrt(GVar(torch.ones(lat_dim)) - torch.pow(w_var, 2))
            orth_term = v * scale_factr
            muscale = this_mu * w_var
            sampled_vec = orth_term + muscale
            sampled_vecs[b] = sampled_vec
            # sampled_vec = torch.FloatTensor(sampled_vec)
            # result.append(sampled_vec)

        return sampled_vecs

    def _sample_weight(self, kappa, dim):
        """Rejection sampling scheme for sampling distance from center on
        surface of the sphere.
        """
        dim = dim - 1  # since S^{n-1}
        b = dim / (np.sqrt(4. * kappa ** 2 + dim ** 2) + 2 * kappa)  # b= 1/(sqrt(4.* kdiv**2 + 1) + 2 * kdiv)
        x = (1. - b) / (1. + b)
        c = kappa * x + dim * np.log(1 - x ** 2)  # dim * (kdiv *x + np.log(1-x**2))

        while True:
            z = np.random.beta(dim / 2., dim / 2.)  # concentrates towards 0.5 as d-> inf
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = np.random.uniform(low=0, high=1)
            if kappa * w + dim * np.log(1. - x * w) - c >= np.log(
                    u):  # thresh is dim *(kdiv * (w-x) + log(1-x*w) -log(1-x**2))
                return w

    def _sample_orthonormal_to(self, mu, dim):
        """Sample point on sphere orthogonal to mu.
        """
        v = GVar(torch.randn(dim))
        rescale_value = mu.dot(v) / mu.norm()
        proj_mu_v = mu * rescale_value.expand(dim)
        ortho = v - proj_mu_v
        ortho_norm = torch.norm(ortho)
        return ortho / ortho_norm.expand_as(ortho)
