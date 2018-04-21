import numpy as np
import torch
from scipy import special as sp

from NVLL.util.util import GVar


class unif_vMF(torch.nn.Module):
    def __init__(self, hid_dim, lat_dim, kappa=1,norm_func=False):
        super().__init__()
        self.hid_dim = hid_dim
        self.lat_dim = lat_dim
        self.kappa = kappa
        # self.func_kappa = torch.nn.Linear(hid_dim, lat_dim)
        self.func_mu = torch.nn.Linear(hid_dim, lat_dim)
        if norm_func:
            self.func_norm = torch.nn.Linear(hid_dim, 1)

        self.noise_scaler = kappa
        self.norm_eps = 1
        self.norm_max = 2
        self.norm_clip = torch.nn.Hardtanh(0, self.norm_max - self.norm_eps)

        self.norm_func = norm_func


        # KLD accounts for both VMF and uniform parts
        kld_value = unif_vMF._vmf_kld(kappa, lat_dim) \
                    + unif_vMF._uniform_kld(0., self.norm_eps, 0., self.norm_max)
        self.kld = GVar(torch.from_numpy(np.array([kld_value])).float())
        print('KLD: {}'.format(self.kld))

    def estimate_param(self, latent_code):
        """
        Compute z_dir and z_norm for vMF.
        norm_func means using another NN to compute the norm (batchsz, 1)
        :param latent_code: batchsz, hidden size
        :return: dict with kappa, mu(batchsz, lat_dim), norm (duplicate in row) (batchsz, lat_dim), (opt)redundant_norm
        """
        ret_dict = {}
        ret_dict['kappa'] = self.kappa
        if self.norm_func:
            mu = self.func_mu(latent_code)
            # Use additional function to compute z_norm
            mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
            ret_dict['mu'] = mu

            norm = self.func_norm(latent_code)      # TODO guarantee norm>0?
            clipped_norm = self.norm_clip(norm)
            redundant_norm = torch.max(norm - clipped_norm, torch.zeros_like(norm))
            ret_dict['norm'] = clipped_norm.expand_as(mu)
            ret_dict['redundant_norm'] = redundant_norm
        else:
            # Only compute mu
            mu = self.func_mu(latent_code)

            norm = torch.norm(mu, 2, 1, keepdim=True)
            standard_norm = torch.ones_like(norm)



            redundant_norm = torch.pow(standard_norm - norm,2)
            ret_dict['norm'] = standard_norm.expand_as(mu)
            ret_dict['redundant_norm'] = redundant_norm

            mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
            ret_dict['mu'] = mu
        return ret_dict

    def compute_KLD(self, tup, batch_sz):
        return self.kld.expand(batch_sz)

    @staticmethod
    def _vmf_kld(k, d):
        tmp = (k * ((sp.iv(d / 2.0 + 1.0, k) + sp.iv(d / 2.0, k) * d / (2.0 * k)) / sp.iv(d / 2.0, k) - d / (2.0 * k)) \
               + d * np.log(k) / 2.0 - np.log(sp.iv(d / 2.0, k)) \
               - sp.loggamma(d / 2 + 1) - d * np.log(2) / 2).real
        return tmp

    @staticmethod
    # KL divergence of Unix([x1,x2]) || Unif([y1,y2]), where [x1,x2] should be a subset of [y1,y2]
    def _uniform_kld(x1, x2, y1, y2):
        if x1 < y1 or x2 > y2:
            raise Exception("KLD is infinite: Unif([" + repr(x1) + "," + repr(x2) + "])||Unif([" + repr(y1) + "," + repr(y2) + "])")
        return np.log((y2 - y1)/(x2 - x1))

    def build_bow_rep(self, lat_code, n_sample):
        batch_sz = lat_code.size()[0]
        tup = self.estimate_param(latent_code=lat_code)
        mu = tup['mu']
        norm = tup['norm']
        kappa = tup['kappa']

        kld = self.compute_KLD(tup, batch_sz)
        vecs = []
        if n_sample == 1:
            return tup, kld, self.sample_cell(mu,norm, kappa)
        for n in range(n_sample):
            sample = self.sample_cell(mu,norm, kappa)
            vecs.append(sample)
        vecs = torch.cat(vecs, dim=0)
        return tup, kld, vecs

    def sample_cell(self, mu,norm, kappa):
        """

        :param mu: z_dir (batchsz, lat_dim) . ALREADY normed.
        :param norm: z_norm (batchsz, lat_dim).
        :param kappa: scalar
        :return:
        """
        """vMF sampler in pytorch.
        http://stats.stackexchange.com/questions/156729/sampling-from-von-mises-fisher-distribution-in-python
        Args:
            mu (Tensor): of shape (batch_size, 2*word_dim)
            kappa (Float): controls dispersion. kappa of zero is no dispersion.
        """
        batch_size, id_dim = mu.size()

        # print(torch.norm(mu, 2, 1)) == 1

        result_list = []
        for i in range(batch_size):

            norm_with_noise = self.add_norm_noise(norm[i], self.norm_eps)

            if float(mu[i].norm().data.cpu().numpy()) > 1e-10:
                # sample offset from center (on sphere) with spread kappa
                w = self._sample_weight(kappa, id_dim)
                wtorch = GVar(w * torch.ones(id_dim))

                # sample a point v on the unit sphere that's orthogonal to mu
                v = self._sample_orthonormal_to(mu[i], id_dim)

                # compute new point
                scale_factr = torch.sqrt(GVar(torch.ones(id_dim)) - torch.pow(wtorch, 2))
                orth_term = v * scale_factr
                muscale = mu[i] * wtorch
                sampled_vec = (orth_term + muscale) * norm_with_noise
            else:
                rand_draw = GVar(torch.randn(id_dim))
                rand_draw = rand_draw / torch.norm(rand_draw, p=2).expand(id_dim)
                rand_norms = (torch.rand(1) * self.norm_eps).expand(id_dim)
                sampled_vec = rand_draw * GVar(rand_norms)  # mu[i]
            result_list.append(sampled_vec)

        return torch.stack(result_list, 0).unsqueeze(0)

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

    def add_norm_noise(self, munorm, eps):
        """
        KL loss is - log(maxvalue/eps)
        cut at maxvalue-eps, and add [0,eps] noise.
        """
        # if np.random.rand()<0.05:
        #     print(munorm[0])
        trand = torch.rand(1).expand(munorm.size()) * eps
        return munorm + GVar(trand)
