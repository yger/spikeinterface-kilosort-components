import numpy as np

from spikeinterface.sortingcomponents.matching.base import BaseTemplateMatching, _base_matching_dtype

try:
    import torch

    HAVE_TORCH = True
    from torch.nn.functional import conv1d, max_pool2d, max_pool1d
except ImportError:
    HAVE_TORCH = False


spike_dtype = _base_matching_dtype


class KiloSortMatching(BaseTemplateMatching):
    """
    This code is an adaptation from the code hosted on
    https://github.com/MouseLand/Kilosort/blob/main/kilosort/template_matching.py
    and implementing the KiloSort4 spike sorting algorithm, published here
    https://www.nature.com/articles/s41592-024-02232-7
    by Marius Patchitariu and collaborators

    This code can only used in the context of spikeinterface.sortingcomponents in the context of the nodepipeline.

    This is a torch implementation of a Matching Pursuit Algorithm, with precomputing
    of all pairwise interactions between the templates in a dense manner. To speed up
    the computations of the convolutions, templates are described via a common predefined
    set of temporal components. This allow to compute only once the temporal convolutions
    for these components, and to reconstruct via spatial components the full convolutions.


    To build these spatial and temporal components, one should access spikes from the
    recording, with the following code:

    ..code::

        from spikeinterface.sortingcomponents.tools import get_prototype_and_waveforms
        prototype, wfs, _ = get_prototype_and_waveforms(recording, ms_before=1, ms_after=2)
        import numpy as np
        n_components = 5

        from sklearn.cluster import KMeans
        wfs /= np.linalg.norm(wfs, axis=1)[:, None]
        model = KMeans(n_clusters=n_components, n_init=10).fit(wfs)
        temporal_components = model.cluster_centers_
        temporal_components = temporal_components / np.linalg.norm(temporal_components[:, None])
        temporal_components = temporal_components.astype(np.float32)
        
        from sklearn.decomposition import TruncatedSVD
        model = TruncatedSVD(n_components=n_components).fit(wfs)
        spatial_components = model.components_.astype(np.float32)

    """

    def __init__(
        self,
        recording,
        return_output=True,
        parents=None,
        templates=None,
        temporal_components=None,
        spatial_components=None,
        Th=8,
        max_iter=100,
        engine="torch",
        torch_device="cpu",
        shared_memory=True
    ):

        import scipy
        
        BaseTemplateMatching.__init__(self, recording, templates, return_output=True, parents=None)
        self.templates_array = self.templates.get_dense_templates()
        self.spatial_components = spatial_components
        self.temporal_components = temporal_components
        self.Th = Th
        self.max_iter = max_iter
        self.engine = engine
        self.torch_device = torch_device
        self.shared_memory = shared_memory

        self.num_components = len(self.temporal_components)
        self.num_templates = len(self.templates_array)
        self.num_channels = recording.get_num_channels()
        self.num_samples = self.templates_array.shape[1]

        U = np.zeros((self.num_templates, self.num_channels, self.num_components), dtype=np.float32)
        for i in range(self.num_templates):
            U[i] = np.dot(spatial_components, self.templates_array[i]).T

        Uex = np.einsum("xyz, zt -> xty", U, self.spatial_components)
        if self.engine == "torch":
            Uex = torch.as_tensor(Uex, device=self.torch_device)
            temporal_components_torch = torch.as_tensor(temporal_components, device=self.torch_device)
            X = Uex.reshape(-1, self.num_channels).T
            X = conv1d(X.unsqueeze(1), temporal_components_torch.unsqueeze(1), padding=self.num_samples // 2)
            X = X[:, :, : self.num_templates * self.num_samples]
            Xmax = X.abs().max(0)[0].max(0)[0].reshape(-1, self.num_samples)
            imax = torch.argmax(Xmax, 1)
            Unew_torch = Uex.clone()
            for j in range(self.num_samples):
                ix = imax == j
                Unew_torch[ix] = torch.roll(Unew_torch[ix], self.num_samples // 2 - j, -2)
            U = torch.einsum(
                "xty, zt -> xzy", Unew_torch, torch.as_tensor(spatial_components, device=self.torch_device)
            )
            W = torch.as_tensor(self.spatial_components, device=self.torch_device)
            WtW = conv1d(
                W.reshape(-1, 1, self.num_samples),
                W.reshape(-1, 1, self.num_samples),
                padding=self.num_samples,
            )
            WtW = torch.flip(
                WtW,
                [
                    2,
                ],
            )
            UtU = torch.einsum("ikl, jml -> ijkm", U, U)
            ctc = torch.einsum("ijkm, kml -> ijl", UtU, WtW)
            trange = torch.arange(-self.num_samples, self.num_samples + 1, device=self.torch_device)

            nm = (U**2).sum(-1).sum(-1)

            # put attribute into numpy
            self.U = U.numpy()
            self.W = W.numpy()
            self.trange = trange.numpy()
            self.ctc = ctc.numpy()
            self.nm = nm.numpy()

        else:
            X = Uex.reshape(-1, self.num_channels).T
            X = scipy.signal.oaconvolve(X[:, None, :], self.temporal_components[None, :, ::-1], mode="full", axes=2)
            X = X[:, :, self.num_samples // 2 : self.num_samples // 2 + self.num_samples * self.num_templates]
            Xmax = np.abs(X).max(0).max(0).reshape(-1, self.num_samples)
            imax = np.argmax(Xmax, 1)
            Unew = Uex.copy()
            for j in range(self.num_samples):
                ix = imax == j
                Unew[ix] = np.roll(Unew[ix], self.num_samples // 2 - j, -2)

            self.U = np.einsum("xty, zt -> xzy", Unew, spatial_components)
            self.W = self.spatial_components
            WtW = scipy.signal.oaconvolve(self.W[None, :, ::-1], self.W[:, None, :], mode="full", axes=2)
            WtW = np.flip(WtW, 2)
            UtU = np.einsum("ikl, jml -> ijkm", self.U, self.U)
            self.ctc = np.einsum("ijkm, kml -> ijl", UtU, WtW)
            self.trange = np.arange(-self.num_samples, self.num_samples + 1, device=self.torch_device)

        if self.shared_memory:
            from spikeinterface.core.core_tools import make_shared_array
            arr, shm = make_shared_array(self.ctc.shape, dtype=np.float32)
            arr[:] = self.ctc
            self.ctc = arr
            self.shm = shm

        self.nbefore = self.templates.nbefore
        self.nafter = self.templates.nafter
        self.margin = self.num_samples
        

        self.is_pushed = False

    def get_trace_margin(self):
        return self.margin

    def clean(self):
        if self.shared_memory and self.shm is not None:
            self.ctc = None
            self.shm.close()
            self.shm.unlink()
            self.shm = None

    def __del__(self):
        if self.shared_memory and self.shm is not None:
            self.ctc = None
            self.shm.close()
            self.shm = None

    def _push_to_torch(self):
        # this is a trick to delay the creation of tensor on GPU to enable multi processing
        if self.engine == "torch":
            self.U = torch.as_tensor(self.U, device=self.torch_device)
            self.W = torch.as_tensor(self.W, device=self.torch_device)
            self.trange = torch.as_tensor(self.trange, device=self.torch_device)
            self.ctc = torch.as_tensor(self.ctc, device=self.torch_device)
            self.nm = torch.as_tensor(self.nm, device=self.torch_device)
        self.is_pushed = True


    def compute_matching(self, traces, start_frame, end_frame, segment_index):

        if not self.is_pushed:
            self._push_to_torch()

        if self.engine == "torch":
            X = torch.as_tensor(traces.T, device=self.torch_device)
            B = conv1d(X.unsqueeze(1), self.W.unsqueeze(1), padding=self.num_samples // 2)
            B = torch.einsum("ijk, kjl -> il", self.U, B)

            spikes = np.empty(traces.size, dtype=spike_dtype)
            k = 0

            for t in range(self.max_iter):
                Cf = torch.relu(B) ** 2 / self.nm.unsqueeze(-1)
                Cf[:, : self.margin] = 0
                Cf[:, -self.margin :] = 0

                Cfmax, imax = torch.max(Cf, 0)
                Cmax = max_pool1d(
                    Cfmax.unsqueeze(0).unsqueeze(0), (2 * self.num_samples + 1), stride=1, padding=(self.num_samples)
                )
                cnd1 = Cmax[0, 0] > self.Th**2
                cnd2 = torch.abs(Cmax[0, 0] - Cfmax) < 1e-9
                xs = torch.nonzero(cnd1 * cnd2)

                if len(xs) == 0:
                    break

                iX = xs[:, :1]
                iY = imax[iX]

                nsp = len(iX)
                spikes[k : k + nsp]["sample_index"] = iX[:, 0].cpu()
                spikes[k : k + nsp]["cluster_index"] = iY[:, 0].cpu()
                amp = B[iY, iX] / self.nm[iY]

                n = 2
                for j in range(n):
                    B[:, iX[j::n] + self.trange] -= amp[j::n] * self.ctc[:, iY[j::n, 0], :]

                spikes[k : k + nsp]["amplitude"] = amp[:, 0].cpu()
                k += nsp

            spikes = spikes[:k]
            spikes["channel_index"] = 0
            spikes["sample_index"] += (self.nbefore - self.nafter) // 2
            order = np.argsort(spikes["sample_index"])
            spikes = spikes[order]

        else:
            # B = scipy.signal.oaconvolve(traces.T[np.newaxis, :, :], self.W[:, None, ::-1], mode="full", axes=2)
            # B = np.einsum("ijk, kjl -> il", self.U, B)
            raise NotImplementedError("Not implemented yet")

        return spikes