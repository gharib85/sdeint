# Copyright 2015 Matthew J. Aburn
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version. See <http://www.gnu.org/licenses/>.

"""Numerical integration algorithms for Ito and Stratonovich stochastic
ordinary differential equations.

Usage:
    itoint(f, G, y0, tspan)  for Ito equation dy = f dt + G dW
    stratint(f, G, y0, tspan)  for Stratonovich equation dy = f dt + G \circ dW

    y0 is the initial value
    tspan is an array of time values (currently these must be equally spaced)
    function f is the deterministic part of the system (scalar or  dx1  vector)
    function G is the stochastic part of the system (scalar or  d x m matrix)

sdeint will choose an algorithm for you. Or you can choose one explicitly:

itoEuler: the Euler-Maruyama algorithm for Ito equations.
stratHeun: the Stratonovich Heun algorithm for Stratonovich equations.
itoSRI2: the Roessler2010 order 1.0 strong Stochastic Runge-Kutta
  algorithm SRI2 for Ito equations.
stratSRS2: the Roessler2010 order 1.0 strong Stochastic Runge-Kutta
  algorithm SRS2 for Stratonovich equations.
stratKP2iS: the Kloeden and Platen two-step implicit order 1.0 strong algorithm
  for Stratonovich equations.
"""

from __future__ import absolute_import
from .wiener import deltaW, Ikpw, Iwik, Jkpw, Jwik
import numpy as np
import numbers
from numpy import linalg as la

def der(f, y, i, t, eps = 1e-20):
    """Derivative approximation.

    Args:
      f: callable(y, t) for which to evaluate derivative
      y: array of shape (d,) where to evaluate derivative
      i: index of input to take partial derivative
      t: time
      eps: precision for numerical derivative

    Returns:
      numerical partial derivative of f at (y,t)

    """
    y = np.array(y, dtype=complex)
    y[i] += 1j*eps
    return (f(y, t)).imag / eps


def gen_H_numerical(G, eps = 1e-20):
    """Generate tensor for second order Milstein correction numerically.

    Args:
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      eps: precision for numerical derivative

    """
    def H_num(y, t, eps = eps):
        DG = np.stack([der(G, y, i, t, eps) for i in range(len(y))])
        return np.swapaxes(np.tensordot(G(y,t), DG, axes=([0,0])), 0, 1)
    return H_num


class Error(Exception):
    pass


class SDEValueError(Error):
    """Thrown if integration arguments fail some basic sanity checks"""
    pass


def _check_args(f, G, y0, tspan, dW=None, IJ=None, H=None):
    """Do some validation common to all algorithms. Find dimension d and number
    of Wiener processes m.
    """
    if not np.isclose(min(np.diff(tspan)), max(np.diff(tspan))):
        raise SDEValueError('Currently time steps must be equally spaced.')
    # Be flexible to allow scalar equations. convert them to a 1D vector system
    if isinstance(y0, numbers.Number):
        if isinstance(y0, numbers.Integral):
            numtype = np.float64
        else:
            numtype = type(y0)
        y0_orig = y0
        y0 = np.array([y0], dtype=numtype)
        def make_vector_fn(fn):
            def newfn(y, t):
                return np.array([fn(y[0], t)])
            newfn.__name__ = fn.__name__
            return newfn
        def make_matrix_fn(fn):
            def newfn(y, t):
                return np.array([[fn(y[0], t)]])
            newfn.__name__ = fn.__name__
            return newfn
        if isinstance(f(y0_orig, tspan[0]), numbers.Number):
            f = make_vector_fn(f)
        if isinstance(G(y0_orig, tspan[0]), numbers.Number):
            G = make_matrix_fn(G)
    # determine dimension d of the system
    d = len(y0)
    if len(f(y0, tspan[0])) != d:
        raise SDEValueError('y0 and f have incompatible shapes.')
    message = """y0 has length %d. So G must either be a single function
              returning a matrix of shape (%d, m), or else a list of m separate
              functions each returning a column of G, with shape (%d,)""" % (
                  d, d, d)
    if callable(G):
        # then G must be a function returning a d x m matrix
        Gtest = G(y0, tspan[0])
        if Gtest.ndim != 2 or Gtest.shape[0] != d:
            raise SDEValueError(message)
        # determine number of independent Wiener processes m
        m = Gtest.shape[1]
    else:
        # G should be a list of m functions g_i giving columns of G
        G = tuple(G)
        m = len(G)
        Gtest = np.zeros((d, m))
        for k in range(0, m):
            if not callable(G[k]):
                raise SDEValueError(message)
            Gtestk = G[k](y0, tspan[0])
            if np.shape(Gtestk) != (d,):
                raise SDEValueError(message)
            Gtest[:,k] = Gtestk
    message = """From function G, it seems m==%d. If present, the optional
              parameter dW must be an array of shape (len(tspan)-1, m) giving
              m independent Wiener increments for each time interval.""" % m
    if dW is not None:
        if not hasattr(dW, 'shape') or dW.shape != (len(tspan) - 1, m):
            raise SDEValueError(message)
    message = """From function G, it seems m==%d. If present, the optional
              parameter I or J must be an array of shape (len(tspan)-1, m, m)
              giving an m x m matrix of repeated integral values for each
              time interval.""" % m
    if IJ is not None:
        if not hasattr(IJ, 'shape') or IJ.shape != (len(tspan) - 1, m, m):
            raise SDEValueError(message)
    if H is not None:
        if callable(H):
            # then H must be a function returning a d x m x m tensor
            Htest = H(y0, tspan[0])
            if Htest.shape[0] != d:
                raise SDEValueError("""y0 has length %d, but this does not match
                    the first dimension of H(y0, tspan[0]).""" % d)
            if Htest.shape[1] != Htest.shape[2]:
                raise SDEValueError("""The last two dimensions of H should
                    match, but they are %d, %d""" % (Htest.shape[1], Htest.shape[2] ) )
            if Htest.shape[1] != m:
                raise SDEValueError("""The last two dimensions of H match,
                    but are not equal to m==%d, the second dimension of G""" % d)
        else:
            raise NotImplementedError("Currently H must be callable if used.")
    return (d, m, f, G, y0, tspan, dW, IJ)


def itoint(f, G, y0, tspan, normalized=False):
    """ Numerically integrate Ito equation  dy = f dt + G dW
    """
    # In future versions we can automatically choose here the most suitable
    # Ito algorithm based on properties of the system and noise.
    (d, m, f, G, y0, tspan, __, __) = _check_args(f, G, y0, tspan, None, None)
    chosenAlgorithm = itoSRI2
    return chosenAlgorithm(f, G, y0, tspan, normalized=normalized)


def stratint(f, G, y0, tspan, normalized=False):
    """ Numerically integrate Stratonovich equation  dy = f dt + G \circ dW
    """
    # In future versions we can automatically choose here the most suitable
    # Stratonovich algorithm based on properties of the system and noise.
    (d, m, f, G, y0, tspan, __, __) = _check_args(f, G, y0, tspan, None, None)
    chosenAlgorithm = stratSRS2
    return chosenAlgorithm(f, G, y0, tspan, normalized=normalized)


def itoEuler(f, G, y0, tspan, dW=None, normalized=False, downsample=1):
    """Use the Euler-Maruyama algorithm to integrate the Ito equation
    dy = f(y,t)dt + G(y,t) dW(t)

    where y is the d-dimensional state vector, f is a vector-valued function,
    G is an d x m matrix-valued function giving the noise coefficients and
    dW(t) = (dW_1, dW_2, ... dW_m) is a vector of independent Wiener increments

    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly
      downsample: optional, integer to indicate how frequently to save values.

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      G. Maruyama (1955) Continuous Markov processes and stochastic equations
      Kloeden and Platen (1999) Numerical Solution of Differential Equations
    """
    (d, m, f, G, y0, tspan, dW, __) = _check_args(f, G, y0, tspan, dW, None)
    N = len(tspan)
    N_record = int((N-1)/downsample)+1
    h = (tspan[N-1] - tspan[0])/(N - 1)
    # allocate space for result
    y = np.zeros((N_record, d), dtype=type(y0[0]))
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h)

    y[0] = y0
    y_next = y[0]
    for n in range(0, N-1):
        tn = tspan[n]
        yn = y_next
        dWn = dW[n,:]
        y_next = yn + f(yn, tn)*h + G(yn, tn).dot(dWn)
        if normalized:
            y_next /= la.norm(y_next)
        if n % downsample == 0:
            y[int((n-1)/downsample)+1] = y_next
    return {"trajectory": y}

def itoImplicitEuler(f, G, y0, tspan, dW=None, normalized=False, downsample=1, implicit_type = "implicit"):
    """Use the Implicit Euler-Maruyama algorithm to integrate the Ito equation
    dy = f(y,t)dt + G(y,t) dW(t). The implicit step is taken by using an initial
    approximation from the explicit equation (and repeated once more).

    The implicit step can be taken either for diffusion, drift, or both terms.
    This is specified by the implicit_type argument.

    where y is the d-dimensional state vector, f is a vector-valued function,
    G is an d x m matrix-valued function giving the noise coefficients and
    dW(t) = (dW_1, dW_2, ... dW_m) is a vector of independent Wiener increments

    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly
      downsample: optional, integer to indicate how frequently to save values.
      implicit_type: Which implicit step type to use.
        "implicit", "semi_implicit_drift", or "semi_implicit_diffusion".

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      G. Maruyama (1955) Continuous Markov processes and stochastic equations
      Kloeden and Platen (1999) Numerical Solution of Differential Equations
    """
    (d, m, f, G, y0, tspan, dW, __) = _check_args(f, G, y0, tspan, dW, None)
    assert implicit_type in ["implicit",
                             "semi_implicit_drift",
                             "semi_implicit_diffusion"]
    N = len(tspan)
    N_record = int((N-1)/downsample)+1
    h = (tspan[N-1] - tspan[0])/(N - 1)
    # allocate space for result
    y = np.zeros((N_record, d), dtype=type(y0[0]))
    norms = np.zeros((N_record), dtype=type(y0[0]))
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h)

    def implicit_step(yn, y_next, tn, dWn):
        if implicit_type == "implicit":
            y_next = yn + f(y_next, tn)*h + G(y_next, tn).dot(dWn)
        elif implicit_type == "semi_implicit_drift":
            y_next = yn + f(y_next, tn)*h + G(yn, tn).dot(dWn)
        else:
            y_next = yn + f(yn, tn)*h + G(y_next, tn).dot(dWn)

        norm_next = la.norm(y_next)
        if n % downsample == 0:
            norms[int((n-1)/downsample)+1] = norm_next
        if normalized:
            y_next /= norm_next
        return y_next

    y[0] = y0
    y_next = y[0]
    for n in range(0, N-1):
        tn = tspan[n]
        yn = y_next
        dWn = dW[n,:]

        ## initial approximation using explicit step
        y_next = implicit_step(yn, yn, tn, dWn)

        ## updated approximation using implicit step
        for _ in range(2):
            y_next = implicit_step(yn, y_next, tn, dWn)

        if n % downsample == 0:
            y[int((n-1)/downsample)+1] = y_next
    return {"trajectory": y, "norms": norms}


def itoQuasiImplicitEuler(f, G, y0, tspan, dW=None, normalized=False, downsample=1, implicit_ports = None):
    """Use the Implicit Euler-Maruyama algorithm to integrate the Ito equation
    dy = f(y,t)dt + G(y,t) dW(t), where implicit steps are taken over only ports
    specified as implicit_ports.

    where y is the d-dimensional state vector, f is a vector-valued function,
    G is an d x m matrix-valued function giving the noise coefficients and
    dW(t) = (dW_1, dW_2, ... dW_m) is a vector of independent Wiener increments

    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly
      downsample: optional, integer to indicate how frequently to save values.
      implicit_ports: array of indices
        which noise terms become implicit. The rest are explicit.

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      G. Maruyama (1955) Continuous Markov processes and stochastic equations
      Kloeden and Platen (1999) Numerical Solution of Differential Equations
    """
    (d, m, f, G, y0, tspan, dW, __) = _check_args(f, G, y0, tspan, dW, None)
    explicit_ports = [i for i in range(m) if i not in implicit_ports]
    if implicit_ports is None:
        implicit_ports = []

    N = len(tspan)
    N_record = int((N-1)/downsample)+1
    h = (tspan[N-1] - tspan[0])/(N - 1)
    # allocate space for result
    y = np.zeros((N_record, d), dtype=type(y0[0]))
    norms = np.zeros((N_record), dtype=type(y0[0]))
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h)

    y[0] = y0
    y_next = y[0]
    for n in range(0, N-1):
        tn = tspan[n]
        yn = y_next
        dWn = dW[n,:]
        fn = f(yn, tn)
        Gn = G(yn, tn)
        Ge = Gn[:,explicit_ports]
        Gi = Gn[:,implicit_ports]
        dWn_explicit = dWn[explicit_ports]
        dWn_implicit = dWn[implicit_ports]
        GedWn = Ge.dot(dWn_explicit)
        GidWn = Gi.dot(dWn_implicit)

        ## explicit approximation neglecting all noise
        y_explicit_noise = yn + fn*h + GedWn
        ## initial approximation using explicit step
        y_tilde = y_explicit_noise + GidWn
        if normalized:
            y_tilde /= la.norm(y_tilde)

        ## updated approximation using implicit step on selected noise terms
        y_next = y_explicit_noise + G(y_tilde, tn+h)[:,implicit_ports].dot(dWn_implicit)

        norm_next = la.norm(y_next)
        if n % downsample == 0:
            norms[int((n-1)/downsample)+1] = norm_next
        if normalized:
            y_next /= norm_next
        if n % downsample == 0:
            y[int((n-1)/downsample)+1] = y_next
    return {"trajectory": y, "norms": norms}

def itoMilstein(f, G, H, y0, tspan, Imethod=Ikpw, dW=None, I=None,
    normalized=False, downsample=1):
    """
    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      H: callable(y, t) returning (d,m,m) array
         Tensor-valued function to define the Milstein correction term.
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly
      downsample: optional, integer to indicate how frequently to save values.

    """
    (d, m, f, G, y0, tspan, dW, I) = _check_args(f, G, y0, tspan, dW, I, H)
    N = len(tspan)
    N_record = int((N-1)/downsample)+1
    h = (tspan[N-1] - tspan[0])/(N - 1)
    # allocate space for result
    y = np.zeros((N_record, d), dtype=type(y0[0]))
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h)
    if I is None:
        # pre-generate repeated stochastic integrals for each time step.
        __, I = Imethod(dW, h) # shape (N, m, m)

    y[0] = y0
    y_next = y[0]
    for n in range(0, N-1):
        tn = tspan[n]
        yn = y_next
        dWn = dW[n,:]
        Iij = I[n,:,:]
        fn = f(yn, tn)
        Gn = G(yn, tn)
        Hn = H(yn, tn)
        y_next = (yn + fn*h + Gn.dot(dWn) +
            np.dot(Hn.reshape(d, m**2), Iij.ravel()) )
        if normalized:
            y_next /= la.norm(y_next)
        if n % downsample == 0:
            y[int((n-1)/downsample)+1] = y_next
    return {"trajectory": y}

def numItoMilstein(f, G, y0, tspan, Imethod=Ikpw, dW=None, I=None, normalized=False, downsample=1, eps=1e-20):
    """
    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly

    """
    (d, m, f, G, y0, tspan, dW, I) = _check_args(f, G, y0, tspan, dW, I, None)
    H = gen_H_numerical(G, eps=eps)
    return itoMilstein(f, G, H, y0, tspan, Imethod=Imethod, dW=dW, I=I, normalized=normalized, downsample=downsample)


def stratHeun(f, G, y0, tspan, dW=None, normalized=False):
    """Use the Stratonovich Heun algorithm to integrate Stratonovich equation
    dy = f(y,t)dt + G(y,t) \circ dW(t)

    where y is the d-dimensional state vector, f is a vector-valued function,
    G is an d x m matrix-valued function giving the noise coefficients and
    dW(t) = (dW_1, dW_2, ... dW_m) is a vector of independent Wiener increments

    Args:
      f: callable(y, t) returning (d,) array
         Vector-valued function to define the deterministic part of the system
      G: callable(y, t) returning (d,m) array
         Matrix-valued function to define the noise coefficients of the system
      y0: array of shape (d,) giving the initial state vector y(t==0)
      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.
      dW: optional array of shape (len(tspan)-1, d). This is for advanced use,
        if you want to use a specific realization of the d independent Wiener
        processes. If not provided Wiener increments will be generated randomly

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      W. Rumelin (1982) Numerical Treatment of Stochastic Differential
         Equations
      R. Mannella (2002) Integration of Stochastic Differential Equations
         on a Computer
      K. Burrage, P. M. Burrage and T. Tian (2004) Numerical methods for strong
         solutions of stochastic differential equations: an overview
    """
    (d, m, f, G, y0, tspan, dW, __) = _check_args(f, G, y0, tspan, dW, None)
    N = len(tspan)
    h = (tspan[N-1] - tspan[0])/(N - 1)
    # allocate space for result
    y = np.zeros((N, d), dtype=type(y0[0]))
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h)
    y[0] = y0;
    for n in range(0, N-1):
        tn = tspan[n]
        tnp1 = tspan[n+1]
        yn = y[n]
        dWn = dW[n,:]
        fn = f(yn, tn)
        Gn = G(yn, tn)
        ybar = yn + fn*h + Gn.dot(dWn)
        fnbar = f(ybar, tnp1)
        Gnbar = G(ybar, tnp1)
        y[n+1] = yn + 0.5*(fn + fnbar)*h + 0.5*(Gn + Gnbar).dot(dWn)
        if normalized:
            y[n+1] /= la.norm(y[n+1])
    return {"trajectory": y}


def itoSRI2(f, G, y0, tspan, Imethod=Ikpw, dW=None, I=None, normalized=False, downsample=1):
    """Use the Roessler2010 order 1.0 strong Stochastic Runge-Kutta algorithm
    SRI2 to integrate an Ito equation dy = f(y,t)dt + G(y,t)dW(t)

    where y is d-dimensional vector variable, f is a vector-valued function,
    G is a d x m matrix-valued function giving the noise coefficients and
    dW(t) is a vector of m independent Wiener increments.

    This algorithm is suitable for Ito systems with an arbitrary noise
    coefficient matrix G (i.e. the noise does not need to be scalar, diagonal,
    or commutative). The algorithm has order 2.0 convergence for the
    deterministic part alone and order 1.0 strong convergence for the complete
    stochastic system.

    Args:
      f: A function f(y, t) returning an array of shape (d,)
         Vector-valued function to define the deterministic part of the system

      G: The d x m coefficient function G can be given in two different ways:

         You can provide a single function G(y, t) that returns an array of
         shape (d, m). In this case the entire matrix G() will be evaluated
         2m+1 times at each time step so complexity grows quadratically with m.

         Alternatively you can provide a list of m functions g(y, t) each
         defining one column of G (each returning an array of shape (d,).
         In this case each g will be evaluated 3 times at each time step so
         complexity grows linearly with m. If your system has large m and
         G involves complicated functions, consider using this way.

      y0: array of shape (d,) giving the initial state vector y(t==0)

      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.

      Imethod (callable, optional): which function to use to simulate repeated
        Ito integrals. Here you can choose either sdeint.Ikpw (the default) or
        sdeint.Iwik (which is more accurate but uses a lot of memory in the
        current implementation).

      dW: optional array of shape (len(tspan)-1, d).
      I: optional array of shape (len(tspan)-1, m, m).
        These optional arguments dW and I are for advanced use, if you want to
        use a specific realization of the d independent Wiener processes and
        their multiple integrals at each time step. If not provided, suitable
        values will be generated randomly.

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      A. Roessler (2010) Runge-Kutta Methods for the Strong Approximation of
        Solutions of Stochastic Differential Equations
    """
    return _Roessler2010_SRK2(f, G, y0, tspan, Imethod, dW, I, normalized, downsample)


def stratSRS2(f, G, y0, tspan, Jmethod=Jkpw, dW=None, J=None, normalized=False):
    """Use the Roessler2010 order 1.0 strong Stochastic Runge-Kutta algorithm
    SRS2 to integrate a Stratonovich equation dy = f(y,t)dt + G(y,t)\circ dW(t)

    where y is d-dimensional vector variable, f is a vector-valued function,
    G is a d x m matrix-valued function giving the noise coefficients and
    dW(t) is a vector of m independent Wiener increments.

    This algorithm is suitable for Stratonovich systems with an arbitrary noise
    coefficient matrix G (i.e. the noise does not need to be scalar, diagonal,
    or commutative). The algorithm has order 2.0 convergence for the
    deterministic part alone and order 1.0 strong convergence for the complete
    stochastic system.

    Args:
      f: A function f(y, t) returning an array of shape (d,)
         Vector-valued function to define the deterministic part of the system

      G: The d x m coefficient function G can be given in two different ways:

         You can provide a single function G(y, t) that returns an array of
         shape (d, m). In this case the entire matrix G() will be evaluated
         2m+1 times at each time step so complexity grows quadratically with m.

         Alternatively you can provide a list of m functions g(y, t) each
         defining one column of G (each returning an array of shape (d,).
         In this case each g will be evaluated 3 times at each time step so
         complexity grows linearly with m. If your system has large m and
         G involves complicated functions, consider using this way.

      y0: array of shape (d,) giving the initial state vector y(t==0)

      tspan (array): The sequence of time points for which to solve for y.
        These must be equally spaced, e.g. np.arange(0,10,0.005)
        tspan[0] is the intial time corresponding to the initial state y0.

      Jmethod (callable, optional): which function to use to simulate repeated
        Stratonovich integrals. Here you can choose either sdeint.Jkpw (the
        default) or sdeint.Jwik (which is more accurate but uses a lot of
        memory in the current implementation).

      dW: optional array of shape (len(tspan)-1, d).
      J: optional array of shape (len(tspan)-1, m, m).
        These optional arguments dW and J are for advanced use, if you want to
        use a specific realization of the d independent Wiener processes and
        their multiple integrals at each time step. If not provided, suitable
        values will be generated randomly.

    Returns:
      y: array, with shape (len(tspan), len(y0))
         With the initial value y0 in the first row

    Raises:
      SDEValueError

    See also:
      A. Roessler (2010) Runge-Kutta Methods for the Strong Approximation of
        Solutions of Stochastic Differential Equations
    """
    return _Roessler2010_SRK2(f, G, y0, tspan, Jmethod, dW, J, normalized)


def _Roessler2010_SRK2(f, G, y0, tspan, IJmethod, dW=None, IJ=None, normalized=False, downsample=1):
    """Implements the Roessler2010 order 1.0 strong Stochastic Runge-Kutta
    algorithms SRI2 (for Ito equations) and SRS2 (for Stratonovich equations).

    Algorithms SRI2 and SRS2 are almost identical and have the same extended
    Butcher tableaus. The difference is that Ito repeateded integrals I_ij are
    replaced by Stratonovich repeated integrals J_ij when integrating a
    Stratonovich equation (Theorem 6.2 in Roessler2010).

    Args:
      f: A function f(y, t) returning an array of shape (d,)
      G: Either a function G(y, t) that returns an array of shape (d, m),
         or a list of m functions g(y, t) each returning an array shape (d,).
      y0: array of shape (d,) giving the initial state
      tspan (array): Sequence of equally spaced time points
      IJmethod (callable): which function to use to generate repeated
        integrals. N.B. for an Ito equation, must use an Ito version here
        (either Ikpw or Iwik). For a Stratonovich equation, must use a
        Stratonovich version here (Jkpw or Jwik).
      dW: optional array of shape (len(tspan)-1, d).
      IJ: optional array of shape (len(tspan)-1, m, m).
        Optional arguments dW and IJ are for advanced use, if you want to
        use a specific realization of the d independent Wiener processes and
        their multiple integrals at each time step. If not provided, suitable
        values will be generated randomly.

    Returns:
      y: array, with shape (len(tspan), len(y0))

    Raises:
      SDEValueError

    See also:
      A. Roessler (2010) Runge-Kutta Methods for the Strong Approximation of
        Solutions of Stochastic Differential Equations
    """
    (d, m, f, G, y0, tspan, dW, IJ) = _check_args(f, G, y0, tspan, dW, IJ)
    N = len(tspan)
    have_separate_g = (not callable(G)) # if G is given as m separate functions
    N_record = int((N-1)/downsample)+1
    h = (tspan[N-1] - tspan[0])/(N - 1) # assuming equal time steps
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h) # shape (N, m)
    if IJ is None:
        # pre-generate repeated stochastic integrals for each time step.
        # Must give I_ij for the Ito case or J_ij for the Stratonovich case:
        __, I = IJmethod(dW, h) # shape (N, m, m)
    else:
        I = IJ
    # allocate space for result
    y = np.zeros((N_record, d), dtype=type(y0[0]))
    y[0] = y0;
    Yn1 = y[0]
    Gn = np.zeros((d, m), dtype=y.dtype)
    for n in range(0, N-1):
        tn = tspan[n]
        tn1 = tspan[n+1]
        h = tn1 - tn
        sqrth = np.sqrt(h)
        Yn = Yn1 # shape (d,)
        Ik = dW[n,:] # shape (m,)
        Iij = I[n,:,:] # shape (m, m)

        fnh = f(Yn, tn)*h # shape (d,)
        if have_separate_g:
            for k in range(0, m):
                Gn[:,k] = G[k](Yn, tn)
        else:
            Gn = G(Yn, tn)
        sum1 = Gn.dot(Iij)/sqrth # shape (d, m)
        H20 = Yn + fnh # shape (d,)
        H20b = np.reshape(H20, (d, 1))
        H2 = H20b + sum1 # shape (d, m)
        H3 = H20b - sum1
        fn1h = f(H20, tn1)*h
        Yn1 = Yn + 0.5*(fnh + fn1h) + Gn.dot(Ik)
        if have_separate_g:
            for k in range(0, m):
                Yn1 += 0.5*sqrth*(G[k](H2[:,k], tn1) - G[k](H3[:,k], tn1))
        else:
            for k in range(0, m):
                Yn1 += 0.5*sqrth*(G(H2[:,k], tn1)[:,k] - G(H3[:,k], tn1)[:,k])
        if normalized:
            Yn1 /= la.norm(Yn1)
        if n % downsample == 0:
            y[int((n-1)/downsample)+1] = Yn1
    return {"trajectory": y}

def stratKP2iS(f, G, y0, tspan, Jmethod=Jkpw, gam=None, al1=None, al2=None,
               rtol=1e-4, dW=None, J=None, normalized=False):
    """Use the Kloeden and Platen two-step implicit order 1.0 strong algorithm
    to integrate a Stratonovich equation dy = f(y,t)dt + G(y,t)\circ dW(t)

    This semi-implicit algorithm may be useful for stiff systems. The noise
    does not need to be scalar, diagonal, or commutative.

    This algorithm is defined in Kloeden and Platen (1999) section 12.4,
    equations (4.5) and (4.7). Here implementing that scheme with default
    parameters \gamma_k = \alpha_{1,k} = \alpha_{2,k} = 0.5 for k=1..d using
    MINPACK HYBRD algorithm to solve the implicit vector equation at each step.

    Args:
      f: A function f(y, t) returning an array of shape (d,) to define the
        deterministic part of the system
      G: A function G(y, t) returning an array of shape (d, m) to define the
        noise coefficients of the system
      y0: array of shape (d,) giving the initial state
      tspan (array): Sequence of equally spaced time points
      Jmethod (callable, optional): which function to use to simulate repeated
        Stratonovich integrals. Here you can choose either sdeint.Jkpw (the
        default) or sdeint.Jwik (which is more accurate but uses a lot of
        memory in the current implementation).
      gam, al1, al2 (optional arrays of shape (d,)): These can configure free
        parameters \gamma_k, \alpha_{1,k}, \alpha_{2,k} of the algorithm.
        You can omit these, then the default values 0.5 will be used.
      rtol (float, optional): Relative error tolerance. The default is 1e-4.
        This is the relative tolerance used when solving the implicit equation
        for Y_{n+1} at each step. It does not mean that the overall sample path
        approximation has this relative precision.
      dW: optional array of shape (len(tspan)-1, d).
      J: optional array of shape (len(tspan)-1, m, m).
        These optional arguments dW and J are for advanced use, if you want to
        use a specific realization of the d independent Wiener processes and
        their multiple integrals at each time step. If not provided, suitable
        values will be generated randomly.

    Returns:
      y: array, with shape (len(tspan), len(y0))

    Raises:
      SDEValueError, RuntimeError

    See also:
      P. Kloeden and E. Platen (1999) Numerical Solution of Stochastic
        Differential Equations, revised and updated 3rd printing.
    """
    try:
        from scipy.optimize import fsolve
    except ImportError:
        raise Error('stratKP2iS() requires package ``scipy`` to be installed.')
    (d, m, f, G, y0, tspan, dW, J) = _check_args(f, G, y0, tspan, dW, J)
    if not callable(G):
        raise SDEValueError('G should be a function returning a d x m matrix.')
    if np.iscomplexobj(y0):
        raise SDEValueError("stratKP2iS() can't yet handle complex variables.")
    if gam is None:
        gam = np.ones((d,))*0.5  # Default level of implicitness \gamma_k = 0.5
    if al1 is None:
        al1 = np.ones((d,))*0.5  # Default \alpha_{1,k} = 0.5
    if al2 is None:
        al2 = np.ones((d,))*0.5  # Default \alpha_{2,k} = 0.5
    N = len(tspan)
    h = (tspan[N-1] - tspan[0])/(N - 1) # assuming equal time steps
    if dW is None:
        # pre-generate Wiener increments (for m independent Wiener processes):
        dW = deltaW(N - 1, m, h) # shape (N, m)
    if J is None:
        # pre-generate repeated Stratonovich integrals for each time step
        __, J = Jmethod(dW, h) # shape (N, m, m)
    # allocate space for result
    y = np.zeros((N, d), dtype=type(y0[0]))
    def _imp(Ynp1, Yn, Ynm1, Vn, Vnm1, tnp1, tn, tnm1, fn, fnm1):
        """At each step we will solve _imp(Ynp1, ...) == 0 for Ynp1.
        The meaning of these arguments is: Y_{n+1}, Y_n, Y_{n-1}, V_n, V_{n-1},
        t_{n+1}, t_n, t_{n-1}, f(Y_n, t_n), f(Y_{n-1}, t_{n-1})."""
        return ((1 - gam)*Yn + gam*Ynm1 + (al2*f(Ynp1, tnp1) +
                (gam*al1 + (1 - al2))*fn + gam*(1 - al1)*fnm1)*h + Vn +
                gam*Vnm1 - Ynp1)
    fn = None
    Vn = None
    y[0] = y0;
    for n in range(0, N-1):
        tn = tspan[n]
        tnp1 = tspan[n+1]
        h = tnp1 - tn
        sqrth = np.sqrt(h)
        Yn = y[n] # shape (d,)
        Jk = dW[n,:] # shape (m,)
        Jij = J[n,:,:] # shape (m, m)
        fnm1 = fn
        fn = f(Yn, tn)
        Gn = G(Yn, tn)
        Ybar = (Yn + fn*h).reshape((d, 1)) + Gn*sqrth # shape (d, m)
        sum1 = np.zeros((d,))
        for j1 in range(0, m):
            sum1 += np.dot(G(Ybar[:,j1], tn) - Gn, Jij[j1,:])
        Vnm1 = Vn
        Vn = np.dot(Gn, Jk) + sum1/sqrth
        if n == 0:
            # First step uses Kloeden&Platen explicit order 1.0 strong scheme:
            y[n+1] = Yn + fn*h + Vn
            continue
        tnm1 = tspan[n-1]
        Ynm1 = y[n-1] # shape (d,)
        # now solve _imp(Ynp1, ...) == 0 for Ynp1, near to Yn
        args = (Yn, Ynm1, Vn, Vnm1, tnp1, tn, tnm1, fn, fnm1)
        (Ynp1, __, status, msg) = fsolve(_imp, Yn, args=args, xtol=rtol,
                                         full_output=True)
        if status == 1:
            if normalized:
                Ynp1 /= la.norm(Ynp1)
            y[n+1] = Ynp1
        else:
            m = """At time t_n = %g Failed to solve for Y_{n+1} with args %s.
                Reason: %s""" % (tn, args, msg)
            raise RuntimeError(m)
    return {"trajectory": y}
