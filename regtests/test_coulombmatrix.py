import time
import pytest
import numpy as np
from numpy.random import RandomState
from conftest import (
    assert_matrix_descriptor_exceptions,
    assert_matrix_descriptor_flatten,
    assert_matrix_descriptor_sorted,
    assert_matrix_descriptor_eigenspectrum,
    assert_matrix_descriptor_random,
    assert_no_system_modification,
    assert_sparse,
    assert_parallellization,
    assert_symmetries,
    assert_derivatives,
    big_system,
)
from dscribe.descriptors import CoulombMatrix


# =============================================================================
# Utilities
def cm_python(system, n_atoms_max, permutation, flatten, sigma=None):
    """Calculates a python reference value for the Coulomb matrix."""
    pos = system.get_positions()
    n = len(system)
    distances = np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=-1)
    q = system.get_atomic_numbers()
    qiqj = q[None, :] * q[:, None]
    np.fill_diagonal(distances, 1)
    cm = qiqj / distances
    np.fill_diagonal(cm, 0.5 * q**2.4)
    random_state = RandomState(42)

    # Permutation option
    if permutation == "eigenspectrum":
        eigenvalues = np.linalg.eigvalsh(cm)
        abs_values = np.absolute(eigenvalues)
        sorted_indices = np.argsort(abs_values)[::-1]
        eigenvalues = eigenvalues[sorted_indices]
        padded = np.zeros((n_atoms_max))
        padded[:n] = eigenvalues
    else:
        if permutation == "sorted_l2":
            norms = np.linalg.norm(cm, axis=1)
            sorted_indices = np.argsort(norms, axis=0)[::-1]
            cm = cm[sorted_indices]
            cm = cm[:, sorted_indices]
        elif permutation == "random":
            norms = np.linalg.norm(cm, axis=1)
            noise_norm_vector = random_state.normal(norms, sigma)
            indexlist = np.argsort(noise_norm_vector)
            indexlist = indexlist[::-1]  # Order highest to lowest
            cm = cm[indexlist][:, indexlist]
        elif permutation == "none":
            pass
        else:
            raise ValueError("Unkown permutation option")
        # Flattening
        if flatten:
            cm = cm.flatten()
            padded = np.zeros((n_atoms_max**2))
            padded[: n**2] = cm
        else:
            padded = np.zeros((n_atoms_max, n_atoms_max))
            padded[:n, :n] = cm

    return padded


def coulomb_matrix(**kwargs):
    """Returns a function that can be used to create a valid CoulombMatrix
    descriptor for a dataset.
    """

    def func(systems=None):
        n_atoms_max = None if systems is None else max([len(s) for s in systems])
        final_kwargs = {
            "n_atoms_max": n_atoms_max,
            "permutation": "none",
            "flatten": True,
        }
        final_kwargs.update(kwargs)
        if (
            final_kwargs["permutation"] == "random"
            and final_kwargs.get("sigma") is None
        ):
            final_kwargs["sigma"] = 2
        return CoulombMatrix(**final_kwargs)

    return func


# =============================================================================
# Common tests with parametrizations that may be specific to this descriptor
def test_matrix_descriptor_exceptions():
    assert_matrix_descriptor_exceptions(coulomb_matrix)


def test_matrix_descriptor_flatten():
    assert_matrix_descriptor_flatten(coulomb_matrix)


def test_matrix_descriptor_sorted():
    assert_matrix_descriptor_sorted(coulomb_matrix)


def test_matrix_descriptor_eigenspectrum():
    assert_matrix_descriptor_eigenspectrum(coulomb_matrix)


def test_matrix_descriptor_random():
    assert_matrix_descriptor_random(coulomb_matrix)


@pytest.mark.parametrize(
    "n_jobs, flatten, sparse",
    [
        (1, True, False),  # Serial job, flattened, dense
        (2, True, False),  # Parallel job, flattened, dense
        (2, False, False),  # Unflattened output, dense
        (1, True, True),  # Serial job, flattened, sparse
        (2, True, True),  # Parallel job, flattened, sparse
        (2, False, True),  # Unflattened output, sparse
    ],
)
def test_parallellization(n_jobs, flatten, sparse):
    assert_parallellization(coulomb_matrix, n_jobs, flatten, sparse)


def test_no_system_modification():
    assert_no_system_modification(coulomb_matrix)


def test_sparse():
    assert_sparse(coulomb_matrix)


@pytest.mark.parametrize(
    "permutation_option, translation, rotation, permutation",
    [
        ("none", True, True, False),
        ("eigenspectrum", True, True, True),
        ("sorted_l2", False, False, False),
    ],
)
def test_symmetries(permutation_option, translation, rotation, permutation):
    """Tests the symmetries of the descriptor. Notice that sorted_l2 is not
    guaranteed to have any of the symmetries due to numerical issues with rows
    that have nearly equal norm."""
    assert_symmetries(
        coulomb_matrix(permutation=permutation_option),
        translation,
        rotation,
        permutation,
    )


@pytest.mark.parametrize(
    "permutation, method",
    [
        ("none", "numerical"),
        ("eigenspectrum", "numerical"),
        ("sorted_l2", "numerical"),
    ],
)
def test_derivatives(permutation, method):
    assert_derivatives(coulomb_matrix(permutation=permutation), method)


# =============================================================================
# Tests that are specific to this descriptor.
@pytest.mark.parametrize(
    "permutation, n_features",
    [
        ("none", 25),
        ("eigenspectrum", 5),
        ("sorted_l2", 25),
    ],
)
def test_number_of_features(permutation, n_features):
    desc = CoulombMatrix(n_atoms_max=5, permutation=permutation, flatten=False)
    assert n_features == desc.get_number_of_features()


@pytest.mark.parametrize(
    "permutation",
    [
        ("none"),
        ("eigenspectrum"),
        ("sorted_l2"),
    ],
)
def test_features(permutation, H2O):
    n_atoms_max = 5
    desc = CoulombMatrix(
        n_atoms_max=n_atoms_max, permutation=permutation, flatten=False
    )
    n_features = desc.get_number_of_features()
    cm = desc.create(H2O)
    cm_assumed = cm_python(H2O, n_atoms_max, permutation, False)
    assert np.allclose(cm, cm_assumed)


def test_periodicity(bulk_system):
    """Tests that periodicity is not taken into account in Coulomb matrix
    even if the system is set as periodic.
    """
    desc = CoulombMatrix(n_atoms_max=5, permutation="none", flatten=False)
    cm = desc.create(bulk_system)
    pos = bulk_system.get_positions()
    assumed = 1 * 1 / np.linalg.norm((pos[0] - pos[1]))
    assert cm[0, 1] == assumed


@pytest.mark.parametrize(
    "permutation",
    [
        "none",
        "eigenspectrum",
        "sorted_l2",
        "random",
    ],
)
def test_performance(permutation):
    """Tests that the C++ code performs better than the numpy version."""
    n_iter = 10
    system = big_system()
    times = []
    start = time
    n_atoms_max = len(system)
    descriptor = coulomb_matrix(permutation=permutation)([system])

    # Measure C++ time
    start = time.time()
    for i in range(n_iter):
        descriptor.create(system)
    end = time.time()
    elapsed_cpp = end - start

    # Measure Python time
    start = time.time()
    for i in range(n_iter):
        cm_python(system, n_atoms_max, permutation, True)
    end = time.time()
    elapsed_python = end - start

    assert elapsed_python > elapsed_cpp
