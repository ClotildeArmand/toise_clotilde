
import numpy
from scipy import optimize, stats
import itertools
from copy import copy
from multillh import LLHEval, asimov_llh, get_expectations
from util import *
import logging

def is_zenith_weight(zenith_weight, aeff):
	zenith_dim = aeff.dimensions.index('true_zenith_band')
	# print issubclass(numpy.asarray(zenith_weight).dtype.type, numpy.floating), len(zenith_weight), aeff.values.shape[zenith_dim]
	return issubclass(numpy.asarray(zenith_weight).dtype.type, numpy.floating) and len(zenith_weight) == aeff.values.shape[zenith_dim]

class PointSource(object):
	def __init__(self, effective_area, fluence, zenith_selection, with_energy=True):
		"""
		
		:param effective_area: an effective area
		:param fluence: flux integrated over the energy bins of `effective_area`, in 1/cm^2 s
		:param zenith_selection: if an integer or slice, select only these zenith bins. If a
		    sequence of floats of the same length as the number of zenith angle bins, average
		    over zenith bins with the given weights (for example, to account for motion of a
		    source in local coordinates when the detector is not at Pole)
		"""

		self._edges = effective_area.bin_edges
		
		if is_zenith_weight(zenith_selection, effective_area):
			zenith_dim = effective_area.dimensions.index('true_zenith_band')
			expand = [None]*effective_area.values.ndim
			expand[zenith_dim] = slice(None)
			effective_area = (effective_area.values[...,:-1] * zenith_selection[expand]).sum(axis=zenith_dim)
		else:
			effective_area = effective_area.values[...,zenith_selection,:,:-1]
		expand = [None]*effective_area.ndim
		expand[1] = slice(None)
		if len(fluence.shape) > 1 and fluence.shape[1] > 1:
			expand[2] = slice(None)
		# 1/yr
		rate = fluence[tuple(expand)]*(effective_area*1e4)
		
		assert numpy.isfinite(rate).all()
		
		self._use_energies = with_energy
		
		self._rate = rate
		self._invalidate_cache()
		
	
	def _invalidate_cache(self):
		self._last_gamma = None
		self._last_expectations = None
	
	def expectations(self, ps_gamma=-2, **kwargs):
		
		if self._last_gamma == ps_gamma:
			return self._last_expectations
		
		energy = self._edges[0]
		
		centers = 0.5*(energy[1:] + energy[:-1])
		specweight = (centers/1e3)**(ps_gamma+2)
		
		expand = [None]*(self._rate.ndim)
		expand[1] = slice(None)
		
		# FIXME: this still neglects the opening angle between neutrino and muon
		total = (self._rate*(specweight[expand])).sum(axis=(0,1))
		# assert total.ndim == 2
		
		if not self._use_energies:
			total = total.sum(axis=0)
		
		self._last_expectations = dict(tracks=total)
		self._last_gamma = ps_gamma
		return self._last_expectations

	def differential_chunks(self, decades=1, emin=-numpy.inf, emax=numpy.inf, exclusive=False):
		"""
		Yield copies of self with the neutrino spectrum restricted to *decade*
		decades in energy
		"""
		# now, sum over decades in neutrino energy
		ebins = self._edges[0]
		loge = numpy.log10(ebins)
		bin_range = int(round(decades/(loge[1]-loge[0])))
		
                # when emin is "equal" to an edge in ebins
                # searchsorted sometimes returns inconsistent indices
                # (wrong side). subtract a little fudge factor to ensure
                # we're on the correct side
		lo = ebins.searchsorted(emin-1e-4) 
		hi = min((ebins.searchsorted(emax-1e-4)+1, loge.size))
		
		if exclusive:
			bins = range(lo, hi-1, bin_range)
		else:
			bins = range(lo, hi-1-bin_range)
		
		for i in bins:
			start = i
			stop = start + bin_range
			chunk = copy(self)
			# zero out the neutrino flux outside the given range
			chunk._rate = self._rate.copy()
			chunk._rate[:,:start,...] = 0
			chunk._rate[:,stop:,...] = 0
			e_center = 10**(0.5*(loge[start] + loge[stop]))
			chunk.energy_range = (10**loge[start], 10**loge[stop])
			yield e_center, chunk

class SteadyPointSource(PointSource):
	r"""
	A stead point source of neutrinos.
	
	The unit is the differential flux per neutrino flavor at 1 TeV,
	in units of :math:`10^{-12} \,\, \rm  TeV^{-1} \, cm^{-2} \, s^{-1}`
	
	"""
	def __init__(self, effective_area, livetime, zenith_bin, with_energy=True):
		# reference flux is E^2 Phi = 1e-12 TeV cm^-2 s^-1
		# remember: fluxes are defined as neutrino + antineutrino, so the flux
		# per particle (which we need here) is .5e-12
		def intflux(e, gamma):
			return (e**(1+gamma))/(1+gamma)
		tev = effective_area.bin_edges[0]/1e3
		# 1/cm^2 yr
		fluence = 0.5e-12*(intflux(tev[1:], -2) - intflux(tev[:-1], -2))*livetime*365*24*3600
		
		PointSource.__init__(self, effective_area, fluence, zenith_bin, with_energy)
		self._livetime = livetime

class WBSteadyPointSource(PointSource):
	def __init__(self, effective_area, livetime, zenith_bin, with_energy=True):
		# reference flux is E^2 Phi = 1e-12 TeV^2 cm^-2 s^-1
		# remember: fluxes are defined as neutrino + antineutrino, so the flux
		# per particle (which we need here) is .5e-12
		def intflux(e, gamma):
			return (e**(1+gamma))/(1+gamma)
		tev = effective_area.bin_edges[0]/1e3
		# 1/cm^2 yr
		fluence = 0.5e-12*(intflux(tev[1:], -2) - intflux(tev[:-1], -2))*livetime*365*24*3600
		
		# scale by the WB GRB fluence, normalized to the E^-2 flux between 100 TeV and 10 PeV
		from grb import WaxmannBahcallFluence
		norm = WaxmannBahcallFluence()(effective_area.bin_edges[0][1:])*effective_area.bin_edges[0][1:]**2
		norm /= norm.max()
		fluence *= norm
		
		PointSource.__init__(self, effective_area, fluence, zenith_bin, with_energy)
		self._livetime = livetime

# An astrophysics-style powerlaw, with a positive lower limit, no upper limit,
# and a negative index
class powerlaw_gen(stats.rv_continuous):
    def _argcheck(self, gamma):
        return gamma > 1
    def _pdf(self, x, gamma):
        return (gamma-1)*x**-gamma
    def _cdf(self, x, gamma):
        return (1. - x**(1.-gamma))
    def _ppf(self, p, gamma):
        return (1.-p)**(1./(1.-gamma))
powerlaw = powerlaw_gen(name='powerlaw', a=1.)

class StackedPopulation(PointSource):
	@staticmethod
	def draw_source_strengths(n_sources):
		# draw relative source strengths
		scd = powerlaw(gamma=2.5)
		strengths = scd.rvs(n_sources)
		# scale strengths so that the median of the maximum is at 1
		# (the CDF of the maximum of N iid samples is the Nth power of the individual CDF)
		strengths /= scd.ppf(0.5**(1./n_sources))
		return strengths
	
	@staticmethod
	def draw_sindec(n_sources):
		return numpy.random.uniform(-1, 1, n_sources)
	
	def __init__(self, effective_area, livetime, fluxes, sindecs, with_energy=True):
		"""
		:param n_sources: number of sources
		:param weighting: If 'flux', distribute the fluxes according to an SCD
		                  where the median flux from the brightest source is
		                  1e-12 TeV^2 cm^-2 s^-1. Scaling the normalization of
		                  the model scales this median linearly. If 'equal',
		                  assume the same flux from all sources.
		:param source_sindec: sin(dec) of each of the N sources. If None, draw
		                      isotropically
		"""
		
		# scatter sources through the zenith bands isotropically
		zenith_bins = effective_area.bin_edges[1]
		self.sources_per_band = numpy.histogram(-sindecs, bins=zenith_bins)[0]
		self.flux_per_band = numpy.histogram(-sindecs, bins=zenith_bins, weights=fluxes)[0]
		
		# reference flux is E^2 Phi = 1e-12 TeV^2 cm^-2 s^-1
		# remember: fluxes are defined as neutrino + antineutrino, so the flux
		# per particle (which we need here) is .5e-12
		def intflux(e, gamma):
			return (e**(1+gamma))/(1+gamma)
		tev = effective_area.bin_edges[0]/1e3
		# 1/cm^2 yr
		fluence = 0.5e-12*(intflux(tev[1:], -2) - intflux(tev[:-1], -2))*livetime*365*24*3600
		fluence = numpy.outer(fluence, self.flux_per_band)
		
		super(StackedPopulation, self).__init__(effective_area, fluence, slice(None), with_energy)

from scipy.optimize import bisect
from functools import partial
def source_to_local_zenith(declination, latitude, ct_bins):
    """
    Return the fraction of the day that a source at a given declination spends in each zenith bin
    
    :param declination: source declination in degrees
    :param latitude: observer latitude in degrees
    :param ct_bins: edges of bins in cos(zenith), in ascending order
    """
    dec = numpy.radians(declination)
    lat = numpy.radians(latitude)
    def offset(hour_angle, ct=0):
        "difference between source elevation and bin edge at given hour angle"
        return (numpy.cos(hour_angle)*numpy.cos(dec)*numpy.cos(lat) + numpy.sin(dec)*numpy.sin(lat)) - ct
    # find minimum and maximum elevation
    lo = numpy.searchsorted(ct_bins[1:], offset(numpy.pi))
    hi = numpy.searchsorted(ct_bins[:-1], offset(0))
    hour_angle = numpy.empty(len(ct_bins))
    # source never crosses the band
    hour_angle[:lo+1] = numpy.pi
    hour_angle[hi:] = 0
    # source enters or exits
    hour_angle[lo+1:hi] = map(partial(bisect, offset, 0, numpy.pi), ct_bins[lo+1:hi])

    return abs(numpy.diff(hour_angle))/numpy.pi

def nevents(llh, **hypo):
	"""
	Total number of events predicted by hypothesis *hypo*
	"""
	for k in llh.components:
		if not k in hypo:
			if hasattr(llh.components[k], 'seed'):
				hypo[k] = llh.components[k].seed
			else:
				hypo[k] = 1
	return sum(map(numpy.sum, llh.expectations(**hypo).values()))

def discovery_potential(point_source, diffuse_components, sigma=5., baseline=None, tolerance=1e-2, **fixed):
	"""
	Calculate the scaling of the flux in *point_source* required to discover it
	over the background in *diffuse_components* at *sigma* sigma in 50% of
	experiments.
	
	:param point_source: an instance of :class:`PointSource`
	:param diffuse components: a dict of diffuse components. Each of the values
	    should be a point source background component, e.g. the return value of
	    :meth:`diffuse.AtmosphericNu.point_source_background`, along with any
	    nuisance parameters required to evaluate them.
	:param sigma: the required significance. The method will scale
	    *point_source* to find a median test statistic of :math:`\sigma**2`
	:param baseline: a first guess of the correct scaling. If None, the scaling
	    will be estimated from the baseline number of signal and background
	    events as :math:`\sqrt{n_B} \sigma / n_S`.
	:param tolerance: tolerance for the test statistic
	:param fixed: values to fix for the diffuse components in each fit.
	"""
	critical_ts = sigma**2

	
	components = dict(ps=point_source)
	components.update(diffuse_components)
	def ts(flux_norm):
		"""
		Test statistic of flux_norm against flux norm=0
		"""
		allh = asimov_llh(components, ps=flux_norm, **fixed)
		if len(fixed) == len(diffuse_components):
			return -2*(allh.llh(ps=0, **fixed)-allh.llh(ps=flux_norm, **fixed))
		else:
			null = allh.fit(ps=0, **fixed)
			alternate = allh.fit(ps=flux_norm, **fixed)
			# print null, alternate, -2*(allh.llh(**null)-allh.llh(**alternate))-critical_ts
			return -2*(allh.llh(**null)-allh.llh(**alternate))
	def f(flux_norm):
		return ts(flux_norm)-critical_ts
	if baseline is None:
		# estimate significance as signal/sqrt(background)
		allh = asimov_llh(components, ps=1, **fixed)
		total = nevents(allh, ps=1, **fixed)
		nb = nevents(allh, ps=0, **fixed)
		ns = total-nb
		baseline = min((1000, numpy.sqrt(critical_ts)/(ns/numpy.sqrt(nb))))/10
		baseline = max(((numpy.sqrt(critical_ts)/(ns/numpy.sqrt(nb)))/10, 0.3/ns))
		# logging.getLogger().warn('total: %.2g ns: %.2g nb: %.2g baseline norm: %.2g ts: %.2g' % (total, ns, nb, baseline, ts(baseline)))
	# baseline = 1000
	if baseline > 1e4:
		return numpy.inf
	else:
		# actual = optimize.bisect(f, 0, baseline, xtol=baseline*1e-2)
		actual = optimize.fsolve(f, baseline, xtol=tolerance, factor=1, epsfcn=1)
		allh = asimov_llh(components, ps=actual, **fixed)
		total = nevents(allh, ps=actual, **fixed)
		nb = nevents(allh, ps=0, **fixed)
		ns = total-nb
		logging.getLogger().info("baseline: %.2g actual %.2g ns: %.2g nb: %.2g ts: %.2g" % (baseline, actual, ns, nb, ts(actual)))
		return actual[0]


def events_above(observables, edges, ecutoff):
        n = 0
        for k, edge_k in edges.items():
                cut = numpy.where(edge_k[1][1:] > ecutoff)[0][0]
                n += observables[k].sum(axis=0)[cut:].sum()

        return n


def fc_upper_limit(point_source, diffuse_components, ecutoff=0,
                   cl=0.9, **fixed):
        import ROOT as rt
	components = dict(ps=point_source)
	components.update(diffuse_components)

        llh = asimov_llh(components, ps=1, **fixed)

        exes = get_expectations(llh, ps=1, **fixed)
        ntot = sum([events_above(exes[k], components[k].bin_edges, ecutoff) for k in exes.keys()])
        ns = events_above(exes['ps'], components['ps'].bin_edges, ecutoff)
        nb = ntot - ns

        print 'ns: {}, nb: {}'.format(ns, nb)

        tfc = rt.TFeldmanCousins(cl)
        return tfc.CalculateUpperLimit(nb, nb)/ns


def upper_limit(point_source, diffuse_components, cl=0.9, baseline=None, tolerance=1e-2, **fixed):
	"""
	Calculate the median upper limit on *point_source* given the background
	*diffuse_components*.
	
	:param cl: desired confidence level for the upper limit
	
	The remaining arguments are the same as :func:`discovery_potential`
	"""
	critical_ts = stats.chi2.ppf(cl, 1)
	
	components = dict(ps=point_source)
	components.update(diffuse_components)
	def ts(flux_norm):
		"""
		Test statistic of flux_norm against flux norm=0
		"""
		allh = asimov_llh(components, ps=0, **fixed)
		if len(fixed) == len(diffuse_components):
			return -2*(allh.llh(ps=0, **fixed)-allh.llh(ps=flux_norm, **fixed))
		else:
			return -2*(allh.llh(**allh.fit(ps=0, **fixed))-allh.llh(**allh.fit(ps=flux_norm, **fixed)))
	def f(flux_norm):
		# NB: minus sign, because now the null hypothesis is no source
		return -ts(flux_norm)-critical_ts

	if baseline is None:
		# estimate significance as signal/sqrt(background)
		allh = asimov_llh(components, ps=1, **fixed)
		total = nevents(allh, ps=1, **fixed)
		nb = nevents(allh, ps=0, **fixed)
		ns = total-nb
		baseline = min((1000, numpy.sqrt(critical_ts)/(ns/numpy.sqrt(nb))))/10
		baseline = max(((numpy.sqrt(critical_ts)/(ns/numpy.sqrt(nb)))/10, 0.3/ns))
                logging.getLogger().debug('total: %.2g ns: %.2g nb: %.2g baseline norm: %.2g' % (total, ns, nb, baseline))

	if baseline > 1e4:
		return numpy.inf
	else:
		# actual = optimize.bisect(f, 0, baseline, xtol=baseline*1e-2)
		actual = optimize.fsolve(f, baseline, xtol=tolerance, factor=1, epsfcn=1)
		logging.getLogger().debug("baseline: %.2g actual %.2g" % (baseline, actual))
		allh = asimov_llh(components, ps=actual, **fixed)
		total = nevents(allh, ps=actual, **fixed)
		nb = nevents(allh, ps=0, **fixed)
		ns = total-nb
		logging.getLogger().info("ns: %.2g nb: %.2g" % (ns, nb))
		return actual[0]


def differential_discovery_potential(point_source, diffuse_components, sigma=5, decades=0.5, **fixed):
	"""
	Calculate the discovery potential in the same way as :func:`discovery_potential`,
	but with the *decades*-wide chunks of the flux due to *point_source*.
	"""
	energies = []
	sensitivities = []
	for energy, pschunk in point_source.differential_chunks(decades=decades):
		energies.append(energy)
		sensitivities.append(discovery_potential(pschunk,
		                                         diffuse_components, sigma, baseline,
		                                         tolerance, **fixed))
	return numpy.asarray(energies), numpy.asarray(sensitivities)


def differential_upper_limit(point_source, diffuse_components,
                             cl=0.9, baseline=None, tolerance=1e-2, decades=0.5, **fixed):
	"""
	Calculate the discovery potential in the same way as :func:`discovery_potential`,
	but with the *decades*-wide chunks of the flux due to *point_source*.
	"""
	energies = []
	sensitivities = []
	for energy, pschunk in point_source.differential_chunks(decades=decades):
		energies.append(energy)
		sensitivities.append(upper_limit(pschunk,
		                                 diffuse_components, cl, baseline, tolerance,
		                                 **fixed))
	return numpy.asarray(energies), numpy.asarray(sensitivities)


def differential_fc_upper_limit(point_source, diffuse_components, ecutoff=0,
                                cl=0.9, decades=0.5, **fixed):
	energies = []
	sensitivities = []
	for energy, pschunk in point_source.differential_chunks(decades=decades):
		energies.append(energy)
		sensitivities.append(fc_upper_limit(pschunk,
		                                    diffuse_components,
		                                    ecutoff, cl,
		                                    **fixed))
	return numpy.asarray(energies), numpy.asarray(sensitivities)

