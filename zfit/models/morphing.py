#  Copyright (c) 2021 zfit
import tensorflow as tf
import tensorflow_addons as tfa
import zfit.z.numpy as znp
from zfit import z
from zfit.core.binnedpdf import BaseBinnedPDFV1

from ..core import parameter


@z.function(wraps='tensor')
def spline_interpolator(alpha, alphas, densities):
    alphas = alphas[None, :, None]
    shape = tf.shape(densities[0])
    densities_flat = [znp.reshape(density, [-1]) for density in densities]
    densities_flat = znp.stack(densities_flat, axis=0)
    alpha_shaped = znp.reshape(alpha, [1, -1, 1])
    y_flat = tfa.image.interpolate_spline(
        train_points=alphas,
        train_values=densities_flat[None, ...],
        query_points=alpha_shaped,
        order=2

    )
    y_flat = y_flat[0, 0]
    y = tf.reshape(y_flat, shape)
    return y


class SplineMorphingPDF(BaseBinnedPDFV1):
    _morphing_interpolator = staticmethod(spline_interpolator)

    def __init__(self, alpha, hists, extended=None, norm=None):

        if isinstance(hists, list):
            if len(hists) != 3:
                raise ValueError("If hists is a list, it is assumed to correspond to an alpha of -1, 0 and 1."
                                 f" hists is {hists} and has length {len(hists)}.")
            else:
                hists = {float(i - 1): hist for i, hist in enumerate(hists)}
        self.hists = hists
        self.alpha = alpha
        obs = list(hists.values())[0].space
        if extended is None:  # TODO: yields?
            extended = all(hist.is_extended for hist in hists.values())
            if extended:
                alphas = znp.array(list(self.hists.keys()), dtype=znp.float64)

                def interpolated_yield(params):
                    alpha = params['alpha']
                    densities = tuple(params[f'{i}'] for i in range(len(params) - 1))  # minus alpha, we don't want it
                    return spline_interpolator(alpha=alpha,
                                               alphas=alphas,
                                               densities=densities)

                number = parameter.get_auto_number()
                yields = {f"{i}": hist.get_yield() for i, hist in enumerate(hists.values())}
                yields['alpha'] = alpha
                new_yield = parameter.ComposedParameter(f"AUTOGEN_{number}_interpolated_yield",
                                                        interpolated_yield,
                                                        params=yields
                                                        )
                extended = new_yield
        super().__init__(obs=obs, extended=extended, norm=norm, params={'alpha': alpha},
                         name="LinearMorphing")

    def _counts(self, x, norm):
        densities = [hist.counts(x, norm=norm) for hist in self.hists.values()]
        alphas = znp.array(list(self.hists.keys()), dtype=znp.float64)
        alpha = self.params['alpha']
        y = self._morphing_interpolator(alpha, alphas, densities)
        return y

    def _rel_counts(self, x, norm):
        densities = [hist.rel_counts(x, norm=norm) for hist in self.hists.values()]
        alphas = znp.array(list(self.hists.keys()), dtype=znp.float64)
        alpha = self.params['alpha']
        y = self._morphing_interpolator(alpha, alphas, densities)
        return y

    def _ext_pdf(self, x, norm):
        densities = [hist.ext_pdf(x, norm=norm) for hist in self.hists.values()]
        alphas = znp.array(list(self.hists.keys()), dtype=znp.float64)
        alpha = self.params['alpha']
        y = self._morphing_interpolator(alpha, alphas, densities)
        return y

    def _pdf(self, x, norm):
        densities = [hist.pdf(x, norm=norm) for hist in self.hists.values()]
        alphas = znp.array(list(self.hists.keys()), dtype=znp.float64)
        alpha = self.params['alpha']
        y = self._morphing_interpolator(alpha, alphas, densities)
        return y
