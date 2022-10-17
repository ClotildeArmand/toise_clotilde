from toise.figures import figure_data, figure

from toise import (
    diffuse,
    multillh,
    plotting,
    surface_veto,
    pointsource,
    factory,
)
from toise.cache import ecached, lru_cache

from scipy import stats, optimize
from copy import copy
import numpy as np
from tqdm import tqdm
from functools import partial
from io import StringIO
from itertools import product
from pandas import DataFrame

from .flavor import extract_ts, detector_label
from matplotlib.lines import Line2D
import matplotlib.pyplot as plt


def make_components(aeffs, emin=1e2, emax=1e11):
    # zero out effective area beyond active range
    aeff, muon_aeff = copy(aeffs)
    ebins = aeff.bin_edges[0]
    mask = (ebins[1:] <= emax) & (ebins[:-1] >= emin)
    aeff.values *= mask[[None] + [slice(None)] + [None] * (aeff.values.ndim - 2)]

    atmo = diffuse.AtmosphericNu.conventional(
        aeff, 1, hard_veto_threshold=np.inf, veto_threshold=None
    )
    atmo.prior = lambda v, **kwargs: -((v - 1) ** 2) / (2 * 0.1**2)
    prompt = diffuse.AtmosphericNu.prompt(
        aeff, 1, hard_veto_threshold=np.inf, veto_threshold=None
    )
    prompt.min = 0.5
    prompt.max = 3.0
    astro = diffuse.DiffuseAstro(aeff, 1)
    astro.seed = 2.0
    components = dict(atmo=atmo, prompt=prompt, astro=astro)
    if muon_aeff:
        components["muon"] = surface_veto.MuonBundleBackground(muon_aeff, 1)
    return components

@lru_cache()
def asimov_llh(bundle, seed_flux, decades=1.0 / 3, emin=1e2, emax=1e11):
    components = bundle.get_components()
    comp = components["composite"]
    for c, _ in comp._components.values():
        c.set_flux_func(seed_flux)
    llh = multillh.asimov_llh(components, astro=0, composite=1)

    chunk_llh = multillh.LLHEval(llh.data)
    chunk_llh.components["gamma"] = multillh.NuisanceParam(-2)
    chunk_llh.components.update(
        subdivide(
            bundle, seed_flux=seed_flux, decades=decades, emin=emin, emax=emax
        ).get_components()
    )

def psi_binning():
    factory.set_kwargs(
        psi_bins={k: (0, np.pi) for k in ("tracks", "cascades", "radio")}
    )


@figure_data(setup=psi_binning)
def contour(
    exposures,
    astro=1.36,
    gamma=-2.37,
    clean=False,
):
    """
    Calculate exclusion confidence levels for alternate flavor ratios, assuming
    Wilks theorem.

    :param astro: per-flavor astrophysical normalization at 100 TeV, in 1e-18 GeV^-2 cm^-2 sr^-1 s^-1
    :param gamma: astrophysical spectral index
    :param steps: number of steps along one axis of flavor scan
    :param gamma_step: granularity of optimization in spectral index. if 0, the spectral index is fixed.
    """

    bundle = factory.component_bundle(dict(exposures), make_components)

    components = bundle.get_components()
    components["gamma"] = multillh.NuisanceParam(-2)

    llh = multillh.asimov_llh(components, astro=astro, gamma=gamma)

    norm_axis = np.linspace(0.5, 2.0, 51)
    gamma_axis = np.linspace(-2.6, -2.1, 51)

    params = []

    fixed = {"atmo": 1, "muon": 1, "prompt": 1}

    for g, n in tqdm(product(gamma_axis, norm_axis), total=len(norm_axis)*len(gamma_axis)):
        fit = llh.fit(astro=n, gamma=g, **fixed)
        fit["LLH"] = llh.llh(**fit)
        params.append(fit)

    astro_v, gamma_v, ts = extract_ts(DataFrame(params).set_index(["astro", "gamma"]))

    meta = {
        "astro": astro_v.tolist(),
        "gamma": gamma_v.tolist(),
        "confidence_level": (stats.chi2.cdf(ts.T, 2) * 100).tolist()
    }
    
    return meta

@figure
def contour(datasets):
    ax = plt.gca()
    labels = []
    handles = []
    for i, meta in enumerate(datasets):
        handles.append(Line2D([0], [0], color=f"C{i}"))
        labels.append(detector_label(meta["detectors"]))
        values = meta["data"]
        cs = ax.contour(
            -np.array(values["gamma"]),
            values["astro"],
            np.asarray(values["confidence_level"]).T,
            levels=[
                68, 90,
            ],
            linestyles=['-', '--'],
            colors=handles[-1].get_color(),
        )
    ax.legend(handles, labels)
    ax.set_ylabel("norm")
    ax.set_xlabel("gamma")

    return ax.figure

def northern_only():
    factory.set_kwargs(
        cos_theta=np.linspace(-1, 1, 21)[:12]
    )

@figure_data(setup=(psi_binning, northern_only))
def event_counts(
    exposures,
    astro=1.36,
    gamma=-2.37,
    clean=False,
):
    """
    Calculate exclusion confidence levels for alternate flavor ratios, assuming
    Wilks theorem.

    :param astro: per-flavor astrophysical normalization at 100 TeV, in 1e-18 GeV^-2 cm^-2 sr^-1 s^-1
    :param gamma: astrophysical spectral index
    :param steps: number of steps along one axis of flavor scan
    :param gamma_step: granularity of optimization in spectral index. if 0, the spectral index is fixed.
    """

    bundle = factory.component_bundle(dict(exposures), make_components)

    components = bundle.get_components()
    components["gamma"] = multillh.NuisanceParam(-2)

    nominal = dict(
        astro=astro,
        gamma=gamma,
        atmo=0,
        prompt=0,
        muon=0,
    )

    llh = multillh.asimov_llh(components, astro=astro, gamma=gamma)

    flavors = {
        "astro": {"astro": 1},
        "atmospheric": {"astro": 0, "atmo": 1, "prompt": 1, "muon": 0},
        # "atmospheric": {"astro": 0, "atmo": 1, "prompt": 1, "muon": 1},
    }
    prefix = exposures[0][0]
    energy_thresholds = {
        k[len(prefix) + 1 :]: v[0]._aeff.get_bin_edges("reco_energy")[:-1]
        for k, v in llh.components["astro"]._components.items()
    }

    meta = {"reco_energy_threshold": energy_thresholds, "event_counts": {}}
    for label, values in flavors.items():
        params = dict(nominal)
        params.update(values)
        meta["event_counts"][label] = {
            k[len(prefix) + 1 :]: v.sum(axis=0)[::-1].cumsum()[::-1]
            for k, v in llh.expectations(**params).items()
        }

    return meta