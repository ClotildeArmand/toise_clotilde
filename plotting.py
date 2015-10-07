
import numpy

def stepped_path(edges, bins, cumulative=False):
	"""
	Create a stepped path suitable for histogramming
	
	:param edges: bin edges
	:param bins: bin contents
	"""
	if len(edges) != len(bins)+1:
		raise ValueError("edges must be 1 element longer than bins")
	
	x = numpy.zeros((2*len(edges)))
	y = numpy.zeros((2*len(edges)))
	
	x[0::2], x[1::2] = edges, edges
	y[1:-1:2], y[2::2] = bins, bins
	
	if cumulative is not False:
		if cumulative == '<':
			y = y.cumsum()
		elif cumulative == '>':
			y = y[::-1].cumsum()[::-1]
	
	return x,y

def format_energy(fmt, energy):
	places = int(numpy.log10(energy)/3)*3
	if places == 0:
		unit = 'GeV'
	elif places == 3:
		unit = 'TeV'
	elif places == 6:
		unit = 'PeV'
	elif places == 9:
		unit = 'EeV'
	return (fmt % (energy/10**(places))) + ' ' + unit

def plot_profile2d(profile, x, y, levels=[68, 90, 99], colors='k', **kwargs):
	from scipy.stats import chi2
	import matplotlib.pyplot as plt
	
	xv = numpy.unique(profile[x])
	yv = numpy.unique(profile[y])
	shape = (xv.size, yv.size)
	
	ts = 2*(numpy.nanmax(profile['LLH'])-profile['LLH']).reshape(shape)
	pvalue = chi2.cdf(ts.T, 2)*100
	
	ax = plt.gca()
	cs = ax.contour(xv, yv, pvalue, levels=levels, colors=colors, **kwargs)
	if ax.get_xlabel() == '':
		ax.set_xlabel(x)
	if ax.get_ylabel() == '':
		ax.set_ylabel(y)
	return cs

import palettable
def pretty_style(tex=True):
	style = {
	    u'axes.color_cycle' : palettable.colorbrewer.qualitative.Set1_9.mpl_colors,
	    u'figure.figsize' : (3.375,3.375),
	    u'legend.frameon' : False,
	    u'legend.fontsize': 'small',
	    u'lines.linewidth'    : 1.5,
	    u'grid.linewidth' : 0.1,
	    u'grid.linestyle' : '-',
	    u'axes.titlesize' : 'medium',
	}
	if tex:
		style[u'font.family'] = 'serif'
		style[u'font.serif'] = 'Computer Modern'
		style[u'text.usetex'] = True
	return style

def pretty(*args, **kwargs):
	import matplotlib.pyplot as plt
	return plt.rc_context(pretty_style(*args, **kwargs))
