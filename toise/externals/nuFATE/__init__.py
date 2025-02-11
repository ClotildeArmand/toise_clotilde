from itertools import product

import numpy as np
from toolz import memoize

from .crosssections import DISCrossSection, GlashowResonanceCrossSection
from .earth import get_t_earth

Na = 6.0221415e23


class NeutrinoCascade(object):
    """
    Propagate a neutrino flux through the Earth using the method described in
    the nuFATE_ paper.

    .. _nuFATE: https://arxiv.org/pdf/1706.09895.pdf
    """

    def __init__(self, energy_nodes):
        """
        :param energy_nodes: logarithmically spaced grid of energies (in GeV)
            where the neutrino flux will be evaluated
        """
        assert energy_nodes.ndim == 1
        self.energy_nodes = energy_nodes
        # find logarithmic distance between nodes
        dloge = (np.log(self.energy_nodes[-1]) - np.log(self.energy_nodes[0])) / (
            len(self.energy_nodes) - 1
        )
        # ratio between the interval centered on the node energy and the node energy itself
        self._width = 2 * np.sinh(dloge / 2.0)
        # Comparing with NuFate paper: multiply by E_j (= E_in) to account
        # for log scale, then by E_i^2/E_j^2 to account for variable change
        # phi -> E^2*phi
        ei, ej = np.meshgrid(energy_nodes, energy_nodes)
        self.differential_element = 2 * dloge * (ei**2 / ej)

    def transfer_matrix_element(self, i, flavor, out_flavor, column_density):
        """
        Calculate an element of the transfer matrix, i.e. the fraction of
        neutrions of type `flavor` at energy node `i` that is transferred to
        each other energy node after propagating through `column_density` of
        scattering centers.

        :param i: index of energy node for which to calculate transmission probability
        :param flavor: index of neutrino type
        :param out_flavor: index of outgoing neutrino type
        :param column_density: number density of scattering centers in cm^2
        :returns: an array of shape (column_density,energy_nodes)
        """
        # construct a differential flux that is nonzero in only 1 energy bin
        # and integrates to 1
        flux0 = 1.0 / (self._width * self.energy_nodes[i])
        flux = np.where(np.arange(self.energy_nodes.size) == i, flux0, 0)
        if out_flavor != flavor:
            # initial flux of nue/numu is zero
            flux = np.concatenate([np.zeros_like(flux), flux])

        # decompose flux in the eigenbasis of cascade equation solution
        w, v, ci = self.decompose_in_eigenbasis(flux, flavor, out_flavor)

        # attenuate components
        wf = np.exp(w[..., None] * np.asarray(column_density)[None, ...])
        # transform back to energy basis and pseudo-integrate to obtain a
        # survival probability
        return np.dot(v, wf * ci[..., None]).T / flux0

    def transfer_matrix(self, cos_zenith, depth=0.5):
        """
        Calculate a transfer matrix that can be used to convert a neutrino flux
        at the surface of the Earth to the one observed at a detector under
        `depth` km of ice.

        :param cos_zenith: cosine of angle between neutrino arrival direction and local zenith
        :param depth: depth below the Earth's surface, in km
        :returns: an array of shape (6,6,T,N,N), where T is the broadcast shape
            of `cos_zenith` and `depth`, and N is the number of energy nodes.
            In other words, the array contains a transfer matrix for each
            combination of initial neutrino type, final neutrino type, and
            trajectory.
        """
        # find [number] column density of nucleons along the trajectory in cm^-2
        t = np.atleast_1d(np.vectorize(get_t_earth)(np.arccos(cos_zenith), depth) * Na)

        num = self.energy_nodes.size
        transfer_matrix = np.zeros((6, 6) + t.shape + (num, num))
        for i in range(self.energy_nodes.size):
            # nu_e, nu_mu: CC absorption and NC downscattering
            for flavor in range(4):
                transfer_matrix[flavor, flavor, :, i, :] = self.transfer_matrix_element(
                    i, flavor, flavor, t
                )

            # nu_tau: CC absorption and NC downscattering, plus neutrinos
            # from tau decay
            for flavor in range(4, 6):
                for out_flavor in range(flavor % 2, flavor, 2):
                    secondary, tau = np.hsplit(
                        self.transfer_matrix_element(i, flavor, out_flavor, t), 2
                    )
                    transfer_matrix[flavor, flavor, :, i, :] = tau
                    transfer_matrix[flavor, out_flavor, :, i, :] = secondary

        return transfer_matrix

    def _attenuation_for_flavor(self, flux, flavor, out_flavor, column_density):
        assert np.asarray(flux).shape == self.energy_nodes.shape
        flux0 = self.energy_nodes**2 * flux
        flux = flux0
        if out_flavor != flavor:
            # initial flux of nue/numu is zero
            flux0 = np.concatenate([flux, flux])
            flux = np.concatenate([np.zeros_like(flux), flux])
        # decompose flux in the eigenbasis of cascade equation solution
        w, v, ci = self.decompose_in_eigenbasis(flux, flavor, out_flavor)
        # attenuate components
        wf = np.exp(w[..., None] * np.asarray(column_density)[None, ...])
        # transform back to energy basis and pseudo-integrate to obtain a
        # survival probability
        return np.dot(v, wf * ci[..., None]).T / flux0

    def attenuation(self, flux, cos_zenith, depth=0.5, scale=1):
        """
        Calculate the ratio between the flux at the surface of the Earth to the
        one observed at a detector under `depth` km of ice.

        :param flux: differential flux, in [something]/GeV
        :param cos_zenith: cosine of angle between neutrino arrival direction and local zenith
        :param depth: depth below the Earth's surface, in km
        :returns: an array of shape (6,T,N), where T is the broadcast shape
            of `cos_zenith` and `depth`, and N is the number of energy nodes.
        """
        # find [number] column density of nucleons along the trajectory in cm^-2
        # a higher cross-section is equivalent to a larger column depth
        t = scale * np.atleast_1d(
            np.vectorize(get_t_earth)(np.arccos(cos_zenith), depth) * Na
        )

        flux = np.atleast_2d(flux)
        if flux.shape[0] == 1:
            flux = np.repeat(flux, 6, axis=0)
        assert flux.shape == (6, self.energy_nodes)

        num = self.energy_nodes.size
        flux = np.zeros((6,) + t.shape + (num,))
        # nu_e, nu_mu: CC absorption and NC downscattering
        for flavor in range(4):
            flux[flavor, ...] = self._attenuation_for_flavor(
                flux[flavor, :], flavor, flavor, t
            )

        # nu_tau: CC absorption and NC downscattering, plus neutrinos
        # from tau decay
        for flavor in range(4, 6):
            for out_flavor in range(flavor % 2, flavor, 2):
                secondary, tau = np.hsplit(
                    self._attenuation_for_flavor(
                        flux[flavor, :], flavor, out_flavor, t
                    ),
                    2,
                )
                # one contribution each to nu_e and nu_mu
                flux[out_flavor, ...] += secondary
            # only one contribution to nu_tau
            flux[flavor, ...] += tau

        return flux

    @staticmethod
    @memoize
    def _get_cross_section(flavor, target, channel, secondary_flavor=None):
        if secondary_flavor:
            return DISCrossSection.create_secondary(
                flavor, secondary_flavor, target, channel
            )
        else:
            return DISCrossSection.create(flavor, target, channel)

    @memoize
    def total_cross_section(self, flavor):
        """
        Total interaction cross-section for neutrinos of of type `flavor`

        :returns: an array of length N of cross-sections in cm^2
        """
        assert isinstance(flavor, int) and 0 <= flavor < 6
        total = (
            sum(
                (
                    self._get_cross_section(flavor + 1, target, channel).total(
                        self.energy_nodes
                    )
                    for (target, channel) in product(["n", "p"], ["CC", "NC"])
                ),
                np.zeros_like(self.energy_nodes),
            )
            / 2.0
        )
        if flavor == 1:
            # for nuebar, add Glashow resonance cross-section
            # divide by 2 to account for the average number of electrons per
            # nucleon in an isoscalar medium
            total += GlashowResonanceCrossSection().total(self.energy_nodes) / 2.0
        return total

    @memoize
    def differential_cross_section(self, flavor, out_flavor):
        """
        Differential cross-section for neutrinos of of type `flavor` to produce
        secondary neutrinos of type `out_flavor`

        :returns: an array of shape (N,N) of differential cross-sections in cm^2 GeV^-1
        """
        assert isinstance(flavor, int) and 0 <= flavor < 6
        assert isinstance(out_flavor, int) and 0 <= out_flavor < 6
        assert (flavor % 2) == (
            out_flavor % 2
        ), "no lepton-number-violating interactions"
        e_nu, e_sec = np.meshgrid(self.energy_nodes, self.energy_nodes, indexing="ij")
        total = np.zeros_like(e_nu)
        if out_flavor == flavor:
            total += (
                sum(
                    (
                        self._get_cross_section(
                            flavor + 1, target, channel
                        ).differential(e_nu, e_sec)
                        for (target, channel) in product(["n", "p"], ["NC"])
                    ),
                    total,
                )
                / 2.0
            )
        if flavor == 1:
            # for nuebar, add Glashow resonance cross-section, assuming equal
            # branching ratios to e/mu/tau
            # divide by 2 to account for the average number of electrons per
            # nucleon in an isoscalar medium
            total += GlashowResonanceCrossSection().differential(e_nu, e_sec) / 2.0
        if flavor in (4, 5):
            # for nutau(bar), add regeneration cross-section
            total += (
                sum(
                    (
                        self._get_cross_section(
                            flavor + 1, target, channel, out_flavor + 1
                        ).differential(e_nu, e_sec)
                        for (target, channel) in product(["n", "p"], ["CC"])
                    ),
                    np.zeros_like(e_nu),
                )
                / 2.0
            )
        return total

    def decompose_in_eigenbasis(self, flux, flavor, out_flavor):
        """
        Decompose `flux` in the eigenbasis of the cascade-equation solution

        :returns: (w,v,ci), the eigenvalues, eigenvectors, and coefficients of `flux` in the basis `v`
        """
        w, v = self.get_eigenbasis(flavor, out_flavor)
        ci = np.linalg.solve(v, flux)
        return w, v, ci

    def _sink_matrix(self, flavor):
        """
        Return a matrix with total interaction cross-section on the diagonal

        :param flavor: neutrino type (0-6)
        """
        return np.diag(self.total_cross_section(flavor))

    def _source_matrix(self, flavor, out_flavor):
        """
        Return a matrix with E^2-weighted differential neutrino cross-sections below the diagonal

        :param flavor: incoming neutrino type (0-6)
        :param out_flavor: outgoing neutrino type (0-6)
        """
        return np.triu(
            (
                self.differential_cross_section(flavor, out_flavor)
                * self.differential_element
            ).T,
            1,
        )

    @memoize
    def get_eigenbasis(self, flavor, out_flavor):
        """
        Construct and diagonalize the multiplier on the right-hand side of the
        cascade equation (M in Eq. 6 of the nuFATE paper).

        This is a more concise version of cascade.get_RHS_matrices() from the
        original nuFATE implementation.

        :param flavor: incoming neutrino flavor
        :param out_flavor: outgoing neutrino flavor
        :returns: (eigenvalues,eigenvectors) of M
        """
        downscattering = self._source_matrix(flavor, flavor) - self._sink_matrix(flavor)
        if out_flavor == flavor:
            RHSMatrix = downscattering
        else:
            secondary_production = self._source_matrix(flavor, out_flavor)
            secondary_downscattering = self._source_matrix(
                out_flavor, out_flavor
            ) - self._sink_matrix(out_flavor)
            # in the flavor-mixing case, the right-hand side is:
            # nue/mu NC   nue/mu production
            # 0           nutau NC + regeneration
            RHSMatrix = np.vstack(
                [
                    np.hstack([secondary_downscattering, secondary_production]),
                    np.hstack([np.zeros_like(downscattering), downscattering]),
                ]
            )
        w, v = np.linalg.eig(RHSMatrix)
        return w, v


class NeutrinoCascadeToShowers(NeutrinoCascade):
    def __differential_cross_section(self, enu, ef, flavor, channel):
        return (
            sum(
                (
                    DISCrossSection.create(flavor + 1, target, channel).differential(
                        enu, ef
                    )
                    for target in ["n", "p"]
                ),
                np.zeros_like(ef),
            )
            / 2.0
        )

    def __total_cross_section(self, enu, flavor, channel):
        return (
            sum(
                (
                    DISCrossSection.create(flavor + 1, target, channel).total(enu)
                    for target in ["n", "p"]
                ),
                np.zeros_like(enu),
            )
            / 2.0
        )

    def __differential_final_state_cross_section(self, enu, ef, flavor, channel):
        return (
            sum(
                (
                    DISCrossSection.create_final_state(
                        flavor + 1, target, channel
                    ).differential(enu, ef)
                    for target in ["n", "p"]
                ),
                np.zeros_like(ef),
            )
            / 2.0
        )

    @memoize
    def interaction_density(self, flavor):
        """
        Calculate number of events per meter of ice at each energy in energy_nodes
        :param flavor: 0,1,2,3,4,5 == nue,nuebar,numu,numubar,nutau,nutaubar
        :returns: number of events per meter
        """
        assert isinstance(flavor, int)
        assert 0 <= flavor < 6
        # convert differential cross section (cm^2 GeV^-1) to interaction density (cm^-1)
        # [d\sigma/dE \rho N_A \delta E] = [(cm^2 GeV^-1) (g cm^-3) (g^-1) (GeV) (cm m^-1)] = [m^-1]
        density_factor = 1.020 * Na * 100
        enu, ef = np.meshgrid(self.energy_nodes, self.energy_nodes, indexing="ij")
        # all flavors contribute at least to NC
        xsec = np.where(
            enu - ef > 0,
            self.__differential_cross_section(enu, enu - ef, flavor, "NC"),
            0,
        )
        # for CC numu, we see only the initial cascade
        if flavor in (2, 3):
            xsec += np.where(
                enu - ef > 0,
                self.__differential_cross_section(enu, enu - ef, flavor, "CC"),
                0,
            )
        # for CC nutau, we can see the tau decay (and neglect the initial cascade)
        if flavor in (4, 5):
            xsec += np.where(
                enu - ef > 0,
                self.__differential_final_state_cross_section(
                    enu, enu - ef, flavor, "CC"
                ),
                0,
            )
        # pseudo-integrate over differential cross-sections
        xsec *= self.energy_nodes * self._width
        if flavor < 2:
            # assume CC nu_e goes entirely into visible energy
            xsec += np.diag(self.__total_cross_section(self.energy_nodes, flavor, "CC"))
        if flavor == 1:
            # ditto for GR nu_e_bar
            xsec += np.diag(GlashowResonanceCrossSection().total(self.energy_nodes))

        return xsec * density_factor

    def transfer_matrix(self, cos_zenith, depth=0.5):
        """
        Calculate a transfer matrix that can be used to convert a neutrino flux
        at the surface of the Earth to a rate of showers (m^-1) in a detector
        under `depth` km of ice.

        :param cos_zenith: cosine of angle between neutrino arrival direction and local zenith
        :param depth: depth below the Earth's surface, in km
        :returns: an array of shape (6,N,T,N), where T is the broadcast shape
            of `cos_zenith` and `depth`, and N is the number of energy nodes.
            In other words, the array contains a transfer matrix for each
            combination of initial neutrino type, final neutrino type, and
            trajectory.
        """
        # find [number] column density of nucleons along the trajectory in cm^-2
        t = np.atleast_1d(np.vectorize(get_t_earth)(np.arccos(cos_zenith), depth) * Na)

        num = self.energy_nodes.size
        transfer_matrix = np.zeros((6, num) + t.shape + (num,))
        for i in range(self.energy_nodes.size):
            # nu_e, nu_mu: CC absorption and NC downscattering
            for flavor in range(4):
                transfer_matrix[flavor, i, ...] += np.dot(
                    self.transfer_matrix_element(i, flavor, flavor, t),
                    self.interaction_density(flavor),
                )

            # nu_tau: CC absorption and NC downscattering, plus neutrinos
            # from tau decay
            for flavor in range(4, 6):
                for out_flavor in range(flavor % 2, flavor, 2):
                    secondary, tau = np.hsplit(
                        self.transfer_matrix_element(i, flavor, out_flavor, t), 2
                    )
                    transfer_matrix[flavor, i, ...] += np.dot(
                        secondary, self.interaction_density(out_flavor)
                    )
                    # do not double-count tau contribution
                    if out_flavor == flavor % 2:
                        transfer_matrix[flavor, i, ...] += np.dot(
                            tau, self.interaction_density(flavor)
                        )

        return transfer_matrix
