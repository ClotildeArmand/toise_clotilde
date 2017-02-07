
from icecube.load_pybindings import load_pybindings
load_pybindings(__name__,__path__)
from icecube import photospline
import numpy

class CKNWPassingFraction(object):
	"""
	The calculation used in the first two HESE papers.
	"""
	def __init__(self, base, muon_threshold=1e4, floor=1e-1):
		import os
		tabledir = os.environ['I3_BUILD'] + "/AtmosphericSelfVeto/resources/tables/"
		self.spline = photospline.I3SplineTable(tabledir + '/cknw-atmuprobtable.fits')
		self.base = base
		self.floor = floor
		self.threshold = muon_threshold
	
	def __call__(self, particleType, energy, cos_theta):
		zenith = 180*numpy.arccos(cos_theta)/numpy.pi
		
		E_nu = numpy.where(energy > 5e6, 5e6, numpy.where(energy < 2e4, 2e4, energy))
		slope_threshold = self.threshold/E_nu
		ratio = numpy.array([self.spline.eval([z, s]) for z, s in zip(*numpy.broadcast_arrays(numpy.where(zenith > 80, 80, zenith), slope_threshold))])
		ratio = numpy.where((~(ratio<1))|(zenith>85)|(energy<2e4), 1, ratio)
		# trust nothing.
		ratio = numpy.where(ratio < self.floor, self.floor, ratio)
		
		return ratio

from . import selfveto
from icecube.dataclasses import I3Particle
class AnalyticPassingFraction(object):
	"""
	A combination of the Schoenert et al calculation and an approximate treatment of uncorrelated muons from the rest of the shower.
	"""
	def __init__(self, kind='conventional', veto_threshold=1e3, floor=1e-4):
		"""
		:param kind: either 'conventional' for neutrinos from pion/kaon decay
		             or 'charm' for neutrinos from charmed meson decay
		:param veto_threshold: energy at depth where a single muon is guaranteed
		                       to be rejetected by veto cuts [GeV]
		:param floor: minimum passing fraction (helpful for avoiding numerical
		              problems in likelihood functions)
		"""
		self.kind = kind
		self.veto_threshold = veto_threshold
		self.floor = floor
		self.ct_min = 0.05
		
		self._splines = dict()
		if kind == 'conventional':
			numu = self._create_spline('numu', veto_threshold)
			nue = self._create_spline('nue', veto_threshold)
		elif kind == 'charm':
			numu = self._create_spline('charm', veto_threshold)
			nue = numu
		
		self._eval = dict()
		self._eval[I3Particle.NuMu] = numpy.vectorize(lambda enu, ct, depth: numu.eval([enu, ct, depth]))
		self._eval[I3Particle.NuE] = numpy.vectorize(lambda enu, ct, depth: nue.eval([enu, ct, depth]))
	
	def _create_spline(self, kind, veto_threshold):
		"""
		Parameterize the uncorrelated veto probability as a function of
		neutrino energy, zenith angle, and vertical depth, and cache the result.
		"""
		from icecube import photospline
		import os
		fname = os.path.expandvars('$I3_BUILD/AtmosphericSelfVeto/resources/tables/uncorrelated_veto_prob.%s.%.1e.fits' % (kind, veto_threshold))
		if os.path.exists(fname):
			return photospline.I3SplineTable(fname)
		
		from icecube.photospline import spglam as glam
		from icecube.photospline import splinefitstable
		
		def pad_knots(knots, order=2):
			"""
			Pad knots out for full support at the boundaries
			"""
			pre = knots[0] - (knots[1]-knots[0])*numpy.arange(order, 0, -1)
			post = knots[-1] + (knots[-1]-knots[-2])*numpy.arange(1, order+1)
			return numpy.concatenate((pre, knots, post))
		
		def edges(centers):
			dx = numpy.diff(centers)[0]/2.
			return numpy.concatenate((centers-dx, [centers[-1]+dx]))
		
		log_enu, ct = numpy.linspace(1, 9, 51), numpy.linspace(self.ct_min, 1, 21)
		depth = numpy.linspace(1e3, 3e3, 11)
		depth_g = depth[None,None,:]
		log_enu_g, ct_g = map(numpy.transpose, numpy.meshgrid(log_enu, ct))
		
		pr = numpy.zeros(ct_g.shape + (depth.size,))
		for i,d in enumerate(depth):
			slant = selfveto.overburden(ct_g, d)
			emu = selfveto.minimum_muon_energy(slant, veto_threshold)
			pr[...,i] = selfveto.uncorrelated_passing_rate(10**log_enu_g, emu, ct_g, kind=kind)
		
		centers = [log_enu, ct, depth]
		knots = map(pad_knots, map(edges, centers))
		
		spline = glam.fit(pr, numpy.ones(pr.shape), centers, knots, 2, 1e-16)
		splinefitstable.write(spline, fname)
		return photospline.I3SplineTable(fname)
	
	def __call__(self, particleType, enu, ct, depth, spline=True):
		"""
		Estimate the fraction of atmospheric neutrinos that will arrive without
		accompanying muons from the same air shower.
		
		:param particleType: neutrino type for which to evaluate the veto
		:type particleType: icecube.dataclasses.I3Particle.ParticleType
		:param enu: neutrino energy [GeV]
		:param ct: cosine of the zenith angle
		:param depth: vertical depth [m]
		:param spline: if False, evaluate the uncorrelated veto probability
		               directly. Otherwise, use the much faster B-spline
		               representation.
		"""
		if numpy.isscalar(ct) and not ct > self.ct_min:
			return numpy.array(1.)
		emu = selfveto.minimum_muon_energy(selfveto.overburden(ct, depth), self.veto_threshold)
		
		# Verify that we're using a sane encoding scheme
		assert(abs(I3Particle.NuMuBar) == I3Particle.NuMu)
		particleType = abs(numpy.asarray(particleType))
		if spline:
			pr = numpy.where(particleType==I3Particle.NuMu, self._eval[I3Particle.NuMu](numpy.log10(enu), ct, depth),
				numpy.where(particleType==I3Particle.NuE, self._eval[I3Particle.NuE](numpy.log10(enu), ct, depth), 1))
		else:
			enu, ct, depth = numpy.broadcast_arrays(enu, ct, depth)
			if self.kind == 'conventional':
				pr = numpy.where(particleType==I3Particle.NuMu, selfveto.uncorrelated_passing_rate(enu, emu, ct, kind='numu'),
					numpy.where(particleType==I3Particle.NuE, selfveto.uncorrelated_passing_rate(enu, emu, ct, kind='nue'), 1))
			elif self.kind == 'charm':
				pr = selfveto.uncorrelated_passing_rate(enu, emu, ct, kind=self.kind)
		
		# For NuMu specifically there is a guaranteed accompanying muon.
		# Estimate the passing fraction from the fraction of the decay phase
		# space where the muon has too little energy to make it to depth.
		# NB: strictly speaking this calculation applies only to 2-body
		# decays of pions and kaons, but is at least a conservative estimate
		# for 3-body decays of D mesons.
		direct = selfveto.correlated_passing_rate(enu, emu, ct)
		pr *= numpy.where(particleType==I3Particle.NuMu, direct, 1)
		
		return numpy.where(ct > self.ct_min, numpy.where(pr <= 1, numpy.where(pr >= self.floor, pr, self.floor), 1), 1)
