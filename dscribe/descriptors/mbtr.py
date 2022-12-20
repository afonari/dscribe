# -*- coding: utf-8 -*-
"""Copyright 2019 DScribe developers

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import sys
import math
import numpy as np

import sparse

from ase import Atoms
import ase.data

from dscribe.core import System
from dscribe.descriptors import Descriptor
from dscribe.ext import MBTRWrapper
from dscribe.utils.dimensionality import is1d
import dscribe.utils.geometry


class MBTR(Descriptor):
    """Implementation of the Many-body tensor representation up to :math:`k=3`.

    You can choose which terms to include by providing a dictionary in the
    k1, k2 or k3 arguments. This dictionary should contain information
    under three keys: "geometry", "grid" and "weighting". See the examples
    below for how to format these dictionaries.

    You can use this descriptor for finite and periodic systems. When dealing
    with periodic systems or when using machine learning models that use the
    Euclidean norm to measure distance between vectors, it is advisable to use
    some form of normalization.

    For the geometry functions the following choices are available:

    * :math:`k=1`:

       * "atomic_number": The atomic numbers.

    * :math:`k=2`:

       * "distance": Pairwise distance in angstroms.
       * "inverse_distance": Pairwise inverse distance in 1/angstrom.

    * :math:`k=3`:

       * "angle": Angle in degrees.
       * "cosine": Cosine of the angle.

    For the weighting the following functions are available:

    * :math:`k=1`:

       * "unity": No weighting.

    * :math:`k=2`:

       * "unity": No weighting.
       * "exp": Weighting of the form :math:`e^{-sx}`
       * "inverse_square": Weighting of the form :math:`1/(x^2)`

    * :math:`k=3`:

       * "unity": No weighting.
       * "exp": Weighting of the form :math:`e^{-sx}`
       * "smooth_cutoff": Weighting of the form :math:`f_{ij}f_{ik}`,
         where :math:`f = 1+y(x/r_{cut})^{y+1}-(y+1)(x/r_{cut})^{y}`

    The exponential weighting is motivated by the exponential decay of screened
    Coulombic interactions in solids. In the exponential weighting the
    parameters **threshold** determines the value of the weighting function after
    which the rest of the terms will be ignored. Either the parameter **scale**
    or **r_cut** can be used to determine the parameter :math:`s`: **scale**
    directly corresponds to this value whereas **r_cut** can be used to
    indirectly determine it through :math:`s=-\log()`:. The meaning of
    :math:`x` changes for different terms as follows:

    * :math:`k=2`: :math:`x` = Distance between A->B
    * :math:`k=3`: :math:`x` = Distance from A->B->C->A.

    The inverse square and smooth cutoff function weightings use a cutoff
    parameter **r_cut**, which is a radial distance after which the rest of
    the atoms will be ignored. For both, :math:`x` means the distance between
    A->B. For the smooth cutoff function, additional weighting key **sharpness**
    can be added, which changes the value of :math:`y`. If not, it defaults to `2`.

    In the grid setup *min* is the minimum value of the axis, *max* is the
    maximum value of the axis, *sigma* is the standard deviation of the
    gaussian broadening and *n* is the number of points sampled on the
    grid.

    If flatten=False, a list of dense np.ndarrays for each k in ascending order
    is returned. These arrays are of dimension (n_elements x n_elements x
    n_grid_points), where the elements are sorted in ascending order by their
    atomic number.

    If flatten=True, a sparse.COO sparse matrix is returned. This sparse matrix
    is of size (n_features,), where n_features is given by
    get_number_of_features(). This vector is ordered so that the different
    k-terms are ordered in ascending order, and within each k-term the
    distributions at each entry (i, j, h) of the tensor are ordered in an
    ascending order by (i * n_elements) + (j * n_elements) + (h * n_elements).

    This implementation does not support the use of a non-identity correlation
    matrix.
    """

    def __init__(
        self,
        k1=None,
        k2=None,
        k3=None,
        normalize_gaussians=True,
        normalization="none",
        flatten=True,
        species=None,
        periodic=False,
        sparse=False,
        dtype="float64",
    ):
        """
        Args:
            k1 (dict): Setup for the k=1 term. For example::

                k1 = {
                    "geometry": {"function": "atomic_number"},
                    "grid": {"min": 1, "max": 10, "sigma": 0.1, "n": 50}
                }

            k2 (dict): Dictionary containing the setup for the k=2 term.
                Contains setup for the used geometry function, discretization and
                weighting function. For example::

                    k2 = {
                        "geometry": {"function": "inverse_distance"},
                        "grid": {"min": 0.1, "max": 2, "sigma": 0.1, "n": 50},
                        "weighting": {"function": "exp", "r_cut": 10, "threshold": 1e-2}
                    }

            k3 (dict): Dictionary containing the setup for the k=3 term.
                Contains setup for the used geometry function, discretization and
                weighting function. For example::

                    k3 = {
                        "geometry": {"function": "angle"},
                        "grid": {"min": 0, "max": 180, "sigma": 5, "n": 50},
                        "weighting" : {"function": "exp", "r_cut": 10, "threshold": 1e-3}
                    }

            normalize_gaussians (bool): Determines whether the gaussians are
                normalized to an area of 1. Defaults to True. If False, the
                normalization factor is dropped and the gaussians have the form.
                :math:`e^{-(x-\mu)^2/2\sigma^2}`
            normalization (str): Determines the method for normalizing the
                output. The available options are:

                * "none": No normalization.
                * "l2_each": Normalize the Euclidean length of each k-term
                  individually to unity.
                * "n_atoms": Normalize the output by dividing it with the number
                  of atoms in the system. If the system is periodic, the number
                  of atoms is determined from the given unit cell.
                * "valle_oganov": Use Valle-Oganov descriptor normalization, with
                  system cell volume and numbers of different atoms in the cell.

            flatten (bool): Whether the output should be flattened to a 1D
                array. If False, a dictionary of the different tensors is
                provided, containing the values under keys: "k1", "k2", and
                "k3":
            species (iterable): The chemical species as a list of atomic
                numbers or as a list of chemical symbols. Notice that this is not
                the atomic numbers that are present for an individual system, but
                should contain all the elements that are ever going to be
                encountered when creating the descriptors for a set of systems.
                Keeping the number of chemical speices as low as possible is
                preferable.
            periodic (bool): Set to true if you want the descriptor output to
                respect the periodicity of the atomic systems (see the
                pbc-parameter in the constructor of ase.Atoms).
            sparse (bool): Whether the output should be a sparse matrix or a
                dense numpy array.
            dtype (str): The data type of the output. Valid options are:

                    * ``"float32"``: Single precision floating point numbers.
                    * ``"float64"``: Double precision floating point numbers.
        """
        if sparse and not flatten:
            raise ValueError(
                "Sparse, non-flattened output is currently not supported. If "
                "you want a non-flattened output, please specify sparse=False "
                "in the MBTR constructor."
            )

        supported_dtype = set(("float32", "float64"))
        if dtype not in supported_dtype:
            raise ValueError(
                "Invalid output data type '{}' given. Please use "
                "one of the following: {}".format(dtype, supported_dtype)
            )
        super().__init__(periodic=periodic, flatten=flatten, sparse=sparse, dtype=dtype)
        self.system = None
        self.k1 = k1
        self.k2 = k2
        self.k3 = k3

        # Setup the involved chemical species
        self.species = species

        self.normalization = normalization
        self.normalize_gaussians = normalize_gaussians

        if self.normalization == "valle_oganov" and not periodic:
            raise ValueError(
                "Valle-Oganov normalization does not support non-periodic systems."
            )

        # Initializing .create() level variables
        self._interaction_limit = None

        # Check that weighting function is specified for periodic systems
        if self.periodic:
            if self.k2 is not None:
                valid = False
                weighting = self.k2.get("weighting")
                if weighting is not None:
                    function = weighting.get("function")
                    if function is not None:
                        if function != "unity":
                            valid = True
                if not valid:
                    raise ValueError(
                        "Periodic systems need to have a weighting function."
                    )

            if self.k3 is not None:
                valid = False
                weighting = self.k3.get("weighting")
                if weighting is not None:
                    function = weighting.get("function")
                    if function is not None:
                        if function != "unity":
                            valid = True

                if not valid:
                    raise ValueError(
                        "Periodic systems need to have a weighting function."
                    )

    def check_grid(self, grid):
        """Used to ensure that the given grid settings are valid.

        Args:
            grid(dict): Dictionary containing the grid setup.
        """
        msg = "The grid information is missing the value for {}"
        val_names = ["min", "max", "sigma", "n"]
        for val_name in val_names:
            try:
                grid[val_name]
            except Exception:
                raise KeyError(msg.format(val_name))

        # Make the n into integer
        grid["n"] = int(grid["n"])
        if grid["min"] >= grid["max"]:
            raise ValueError("The min value should be smaller than the max value.")

    @property
    def k1(self):
        return self._k1

    @k1.setter
    def k1(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError(
                        "The given setup contains the following invalid key: {}".format(
                            key
                        )
                    )

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("atomic_number",))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=1. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity",))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=1. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )

            # Check grid
            self.check_grid(value["grid"])
        self._k1 = value

    @property
    def k2(self):
        return self._k2

    @k2.setter
    def k2(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError(
                        "The given setup contains the following invalid key: {}".format(
                            key
                        )
                    )

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("distance", "inverse_distance"))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=2. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity", "exp", "inverse_square"))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=2. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )
                else:
                    if weight_func == "exp":
                        threshold = weighting.get("threshold")
                        if threshold is None:
                            raise ValueError(
                                "Missing value for 'threshold' in the k=2 weighting."
                            )
                        param = weighting.get("scale", weighting.get("r_cut"))
                        if param is None:
                            raise ValueError(
                                "Provide either 'scale' or 'r_cut' in the k=2 weighting."
                            )
                    elif weight_func == "inverse_square":
                        if weighting.get("r_cut") is None:
                            raise ValueError(
                                "Missing value for 'r_cut' in the k=2 weighting."
                            )

            # Check grid
            self.check_grid(value["grid"])
        self._k2 = value

    @property
    def k3(self):
        return self._k3

    @k3.setter
    def k3(self, value):
        if value is not None:

            # Check that only valid keys are used in the setups
            for key in value.keys():
                valid_keys = set(("geometry", "grid", "weighting"))
                if key not in valid_keys:
                    raise ValueError(
                        "The given setup contains the following invalid key: {}".format(
                            key
                        )
                    )

            # Check the geometry function
            geom_func = value["geometry"].get("function")
            if geom_func is not None:
                valid_geom_func = set(("angle", "cosine"))
                if geom_func not in valid_geom_func:
                    raise ValueError(
                        "Unknown geometry function specified for k=2. Please use one of"
                        " the following: {}".format(valid_geom_func)
                    )

            # Check the weighting function
            weighting = value.get("weighting")
            if weighting is not None:
                valid_weight_func = set(("unity", "exp", "smooth_cutoff"))
                weight_func = weighting.get("function")
                if weight_func not in valid_weight_func:
                    raise ValueError(
                        "Unknown weighting function specified for k=2. Please use one of"
                        " the following: {}".format(valid_weight_func)
                    )
                else:
                    if weight_func == "exp":
                        threshold = weighting.get("threshold")
                        if threshold is None:
                            raise ValueError(
                                "Missing value for 'threshold' in the k=3 weighting."
                            )
                        param = weighting.get("scale", weighting.get("r_cut"))
                        if param is None:
                            raise ValueError(
                                "Provide either 'scale' or 'r_cut' in the k=3 weighting."
                            )
                    elif weight_func == "smooth_cutoff":
                        if weighting.get("r_cut") is None:
                            raise ValueError(
                                "Missing value for 'r_cut' in the k=3 weighting."
                            )
            # Check grid
            self.check_grid(value["grid"])
        self._k3 = value

    @property
    def species(self):
        return self._species

    @species.setter
    def species(self, value):
        """Used to check the validity of given atomic numbers and to initialize
        the C-memory layout for them.

        Args:
            value(iterable): Chemical species either as a list of atomic
                numbers or list of chemical symbols.
        """
        # The species are stored as atomic numbers for internal use.
        self._set_species(value)

        # Setup mappings between atom indices and types together with some
        # statistics
        self.atomic_number_to_index = {}
        self.index_to_atomic_number = {}
        for i_atom, atomic_number in enumerate(self._atomic_numbers):
            self.atomic_number_to_index[atomic_number] = i_atom
            self.index_to_atomic_number[i_atom] = atomic_number
        self.n_elements = len(self._atomic_numbers)
        self.max_atomic_number = max(self._atomic_numbers)
        self.min_atomic_number = min(self._atomic_numbers)

    @property
    def normalization(self):
        return self._normalization

    @normalization.setter
    def normalization(self, value):
        """Checks that the given normalization is valid.

        Args:
            value(str): The normalization method to use.
        """
        norm_options = set(("l2_each", "none", "n_atoms", "valle_oganov"))
        if value not in norm_options:
            raise ValueError(
                "Unknown normalization option given. Please use one of the "
                "following: {}.".format(", ".join([str(x) for x in norm_options]))
            )
        self._normalization = value

    def get_k1_axis(self):
        """Used to get the discretized axis for geometry function of the k=1
        term.

        Returns:
            np.ndarray: The discretized axis for the k=1 term.
        """
        start = self.k1["grid"]["min"]
        stop = self.k1["grid"]["max"]
        n = self.k1["grid"]["n"]

        return np.linspace(start, stop, n)

    def get_k2_axis(self):
        """Used to get the discretized axis for geometry function of the k=2
        term.

        Returns:
            np.ndarray: The discretized axis for the k=2 term.
        """
        start = self.k2["grid"]["min"]
        stop = self.k2["grid"]["max"]
        n = self.k2["grid"]["n"]

        return np.linspace(start, stop, n)

    def get_k3_axis(self):
        """Used to get the discretized axis for geometry function of the k=3
        term.

        Returns:
            np.ndarray: The discretized axis for the k=3 term.
        """
        start = self.k3["grid"]["min"]
        stop = self.k3["grid"]["max"]
        n = self.k3["grid"]["n"]

        return np.linspace(start, stop, n)

    def create(self, system, n_jobs=1, only_physical_cores=False, verbose=False):
        """Return MBTR output for the given systems.

        Args:
            system (:class:`ase.Atoms` or list of :class:`ase.Atoms`): One or many atomic structures.
            n_jobs (int): Number of parallel jobs to instantiate. Parallellizes
                the calculation across samples. Defaults to serial calculation
                with n_jobs=1. If a negative number is given, the used cpus
                will be calculated with, n_cpus + n_jobs, where n_cpus is the
                amount of CPUs as reported by the OS. With only_physical_cores
                you can control which types of CPUs are counted in n_cpus.
            only_physical_cores (bool): If a negative n_jobs is given,
                determines which types of CPUs are used in calculating the
                number of jobs. If set to False (default), also virtual CPUs
                are counted.  If set to True, only physical CPUs are counted.
            verbose(bool): Controls whether to print the progress of each job
                into to the console.

        Returns:
            np.ndarray | sparse.COO | list: MBTR for the
            given systems. The return type depends on the 'sparse' and
            'flatten'-attributes. For flattened output a single numpy array or
            sparse.COO matrix is returned. If the output is not flattened,
            dictionaries containing the MBTR tensors for each k-term are
            returned.
        """
        # Combine input arguments
        system = [system] if isinstance(system, Atoms) else system
        inp = [(i_sys,) for i_sys in system]

        # Determine if the outputs have a fixed size
        if self.flatten:
            static_size = [self.get_number_of_features()]
        else:
            static_size = None

        # Create in parallel
        output = self.create_parallel(
            inp,
            self.create_single,
            n_jobs,
            static_size,
            only_physical_cores,
            verbose=verbose,
        )

        return output

    def create_single(self, system):
        """Return the many-body tensor representation for the given system.

        Args:
            system (:class:`ase.Atoms` | :class:`.System`): Input system.

        Returns:
            dict | np.ndarray | sparse.COO: The return type is
            specified by the 'flatten' and 'sparse'-parameters. If the output
            is not flattened, a dictionary containing of MBTR outputs as numpy
            arrays is created. Each output is under a "kX" key. If the output
            is flattened, a single concatenated output vector is returned,
            either as a sparse or a dense vector.
        """
        # Ensuring variables are re-initialized when a new system is introduced
        self.system = system
        self._interaction_limit = len(system)

        # Check that the system does not have elements that are not in the list
        # of atomic numbers
        self.check_atomic_numbers(system.get_atomic_numbers())

        mbtr = {}
        if self.k1 is not None:
            mbtr["k1"], _ = self._get_k1(system, True, False)
        if self.k2 is not None:
            mbtr["k2"], _ = self._get_k2(system, True, False)
        if self.k3 is not None:
            mbtr["k3"], _ = self._get_k3(system, True, False)

        # Handle normalization
        if self.normalization == "l2_each":
            if self.flatten is True:
                for key, value in mbtr.items():
                    i_data = np.array(value.data)
                    i_norm = np.linalg.norm(i_data)
                    mbtr[key] = value / i_norm
            else:
                for key, value in mbtr.items():
                    i_data = value.ravel()
                    i_norm = np.linalg.norm(i_data)
                    mbtr[key] = value / i_norm
        elif self.normalization == "n_atoms":
            n_atoms = len(self.system)
            if self.flatten is True:
                for key, value in mbtr.items():
                    mbtr[key] = value / n_atoms
            else:
                for key, value in mbtr.items():
                    mbtr[key] = value / n_atoms

        # Flatten output if requested
        if self.flatten:
            keys = sorted(mbtr.keys())
            if len(keys) > 1:
                mbtr = np.concatenate([mbtr[key] for key in keys], axis=0)
            else:
                mbtr = mbtr[keys[0]]

            # Make into a sparse array if requested
            if self.sparse:
                mbtr = sparse.COO.from_numpy(mbtr)

        return mbtr

    def get_number_of_features(self):
        """Used to inquire the final number of features that this descriptor
        will have.

        Returns:
            int: Number of features for this descriptor.
        """
        n_features = 0
        n_elem = self.n_elements

        if self.k1 is not None:
            n_k1_grid = self.k1["grid"]["n"]
            n_k1 = n_elem * n_k1_grid
            n_features += n_k1
        if self.k2 is not None:
            n_k2_grid = self.k2["grid"]["n"]
            n_k2 = (n_elem * (n_elem + 1) / 2) * n_k2_grid
            n_features += n_k2
        if self.k3 is not None:
            n_k3_grid = self.k3["grid"]["n"]
            n_k3 = (n_elem * n_elem * (n_elem + 1) / 2) * n_k3_grid
            n_features += n_k3

        return int(n_features)

    def get_location(self, species):
        """Can be used to query the location of a species combination in the
        the flattened output.

        Args:
            species(tuple): A tuple containing a species combination as
                chemical symbols or atomic numbers. The tuple can be for example
                ("H"), ("H", "O") or ("H", "O", "H").

        Returns:
            slice: slice containing the location of the specified species
                combination. The location is given as a python slice-object, that
                can be directly used to target ranges in the output.

        Raises:
            ValueError: If the requested species combination is not in the
                output or if invalid species defined.
        """
        # Check that the corresponding part is calculated
        k = len(species)
        term = getattr(self, "k{}".format(k))
        if term is None:
            raise ValueError(
                "Cannot retrieve the location for {}, as the term k{} has not "
                "been specied.".format(species, k)
            )

        # Change chemical elements into atomic numbers
        numbers = []
        for specie in species:
            if isinstance(specie, str):
                try:
                    specie = ase.data.atomic_numbers[specie]
                except KeyError:
                    raise ValueError("Invalid chemical species: {}".format(specie))
            numbers.append(specie)

        # Change into internal indexing
        numbers = [self.atomic_number_to_index[x] for x in numbers]
        n_elem = self.n_elements

        # k=1
        if len(numbers) == 1:
            n1 = self.k1["grid"]["n"]
            i = numbers[0]
            m = i
            start = int(m * n1)
            end = int((m + 1) * n1)

        # k=2
        if len(numbers) == 2:
            if numbers[0] > numbers[1]:
                numbers = list(reversed(numbers))

            n2 = self.k2["grid"]["n"]
            i = numbers[0]
            j = numbers[1]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of an upper triangular matrix from left to right and top
            # to bottom.
            m = j + i * n_elem - i * (i + 1) / 2

            offset = 0
            if self.k1 is not None:
                n1 = self.k1["grid"]["n"]
                offset += n_elem * n1
            start = int(offset + m * n2)
            end = int(offset + (m + 1) * n2)

        # k=3
        if len(numbers) == 3:
            if numbers[0] > numbers[2]:
                numbers = list(reversed(numbers))

            n3 = self.k3["grid"]["n"]
            i = numbers[0]
            j = numbers[1]
            k = numbers[2]

            # This is the index of the spectrum. It is given by enumerating the
            # elements of a three-dimensional array where for valid elements
            # k>=i. The enumeration begins from [0, 0, 0], and ends at [n_elem,
            # n_elem, n_elem], looping the elements in the order k, i, j.
            m = j * n_elem * (n_elem + 1) / 2 + k + i * n_elem - i * (i + 1) / 2

            offset = 0
            if self.k1 is not None:
                n1 = self.k1["grid"]["n"]
                offset += n_elem * n1
            if self.k2 is not None:
                n2 = self.k2["grid"]["n"]
                offset += (n_elem * (n_elem + 1) / 2) * n2
            start = int(offset + m * n3)
            end = int(offset + (m + 1) * n3)

        return slice(start, end)

    def _make_new_k1map(self, kx_map):
        kx_map = dict(kx_map)
        new_kx_map = {}

        for key, value in kx_map.items():
            new_key = tuple([int(key)])
            new_kx_map[new_key] = np.array(value, dtype=np.float64)

        return new_kx_map

    def _make_new_kmap(self, kx_map):
        kx_map = dict(kx_map)
        new_kx_map = {}

        for key, value in kx_map.items():
            new_key = tuple(int(x) for x in key.split(","))
            new_kx_map[new_key] = np.array(value, dtype=np.float64)

        return new_kx_map

    def _get_k1(self, system, return_descriptor, return_derivatives):
        """Calculates the first order term and/or its derivatives with
        regard to atomic positions.

        Returns:
            1D or 3D ndarray:   K1 values. If flatten=True, returns a 1D array
                                and if flatten=False returns a 2D array.
                                If return_descriptor=False, returns an array of
                                shape (0).
            3D ndarray:         K1 derivatives. If return_derivatives=False,
                                returns an array of shape (0,0,0).
        """
        grid = self.k1["grid"]
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        sigma = grid["sigma"]

        n_elem = self.n_elements
        n_features = n_elem * n

        if return_descriptor:
            # Determine the geometry function
            geom_func_name = self.k1["geometry"]["function"]

            cmbtr = MBTRWrapper(
                self.atomic_number_to_index,
                self._interaction_limit,
                np.zeros((len(system), 3), dtype=int),
            )

            k1 = np.zeros((n_features), dtype=np.float64)
            cmbtr.get_k1(
                k1,
                system.get_atomic_numbers(),
                geom_func_name.encode(),
                b"unity",
                {},
                start,
                stop,
                sigma,
                n,
            )
        else:
            k1 = np.zeros((0), dtype=np.float64)

        if return_derivatives:
            k1_d = np.zeros((self._interaction_limit, 3, n_features), dtype=np.float64)
        else:
            k1_d = np.zeros((0, 0, 0), dtype=np.float64)

        # Denormalize if requested
        if not self.normalize_gaussians:
            max_val = 1 / (sigma * math.sqrt(2 * math.pi))
            k1 /= max_val
            k1_d /= max_val

        # Reshape the output if non-flattened descriptor is requested
        if return_descriptor and not self.flatten:
            k1 = k1.reshape((n_elem, n))

        # Convert to the final output precision.
        if self.dtype == "float32":
            k1 = k1.astype(self.dtype)
            k1_d = k1_d.astype(self.dtype)

        return (k1, k1_d)

    def _get_k2(self, system, return_descriptor, return_derivatives):
        """Calculates the second order term and/or its derivatives with
        regard to atomic positions.

        Returns:
            1D or 3D ndarray:   K2 values. If flatten=True, returns a 1D array
                                and if flatten=False returns a 3D array.
                                If return_descriptor=False, returns an array of
                                shape (0).
            3D ndarray:         K2 derivatives. If return_derivatives=False,
                                returns an array of shape (0,0,0).
        """
        grid = self.k2["grid"]
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        sigma = grid["sigma"]

        # Determine the weighting function and possible radial cutoff
        r_cut = None
        weighting = self.k2.get("weighting")
        parameters = {}
        if weighting is not None:
            weighting_function = weighting["function"]
            if weighting_function == "exp":
                threshold = weighting["threshold"]
                r_cut = weighting.get("r_cut")
                scale = weighting.get("scale")
                if scale is not None and r_cut is None:
                    r_cut = -math.log(threshold) / scale
                elif scale is None and r_cut is not None:
                    scale = -math.log(threshold) / r_cut
                parameters = {b"scale": scale, b"threshold": threshold}
            elif weighting_function == "inverse_square":
                r_cut = weighting["r_cut"]
        else:
            weighting_function = "unity"

        # Determine the geometry function
        geom_func_name = self.k2["geometry"]["function"]

        # If needed, create the extended system
        if self.periodic:
            centers = system.get_positions()
            ext_system, cell_indices = dscribe.utils.geometry.get_extended_system(
                system, r_cut, centers, return_cell_indices=True
            )
            ext_system = System.from_atoms(ext_system)
        else:
            ext_system = System.from_atoms(system)
            cell_indices = np.zeros((len(system), 3), dtype=int)

        cmbtr = MBTRWrapper(
            self.atomic_number_to_index, self._interaction_limit, cell_indices
        )

        # If radial cutoff is finite, use it to calculate the sparse
        # distance matrix to reduce computational complexity from O(n^2) to
        # O(n log(n))
        n_atoms = len(ext_system)
        if r_cut is not None:
            dmat = ext_system.get_distance_matrix_within_radius(r_cut)
            adj_list = dscribe.utils.geometry.get_adjacency_list(dmat)
            dmat_dense = np.full(
                (n_atoms, n_atoms), sys.float_info.max
            )  # The non-neighbor values are treated as "infinitely far".
            dmat_dense[dmat.row, dmat.col] = dmat.data
        # If no weighting is used, the full distance matrix is calculated
        else:
            dmat_dense = ext_system.get_distance_matrix()
            adj_list = np.tile(np.arange(n_atoms), (n_atoms, 1))

        n_elem = self.n_elements
        n_features = int((n_elem * (n_elem + 1) / 2) * n)

        if return_descriptor:
            k2 = np.zeros((n_features), dtype=np.float64)
        else:
            k2 = np.zeros((0), dtype=np.float64)

        if return_derivatives:
            k2_d = np.zeros((self._interaction_limit, 3, n_features), dtype=np.float64)
        else:
            k2_d = np.zeros((0, 0, 0), dtype=np.float64)

        # Generate derivatives for k=2 term
        cmbtr.get_k2(
            k2,
            k2_d,
            return_descriptor,
            return_derivatives,
            ext_system.get_atomic_numbers(),
            ext_system.get_positions(),
            dmat_dense,
            adj_list,
            geom_func_name.encode(),
            weighting_function.encode(),
            parameters,
            start,
            stop,
            sigma,
            n,
        )

        # Denormalize if requested
        if not self.normalize_gaussians:
            max_val = 1 / (sigma * math.sqrt(2 * math.pi))
            k2 /= max_val
            k2_d /= max_val

        # Valle-Oganov normalization is calculated separately for each pair.
        # Not implemented for derivatives.
        if self.normalization == "valle_oganov":
            for i in range(n_elem):
                for j in range(n_elem):
                    if j < i:
                        continue
                    S = self.system
                    n_elements = len(self.species)
                    V = S.cell.volume
                    imap = self.index_to_atomic_number
                    # Calculate the amount of each element for N_A*N_B term
                    counts = {}
                    for index, number in imap.items():
                        counts[index] = list(S.get_atomic_numbers()).count(number)
                    if i == j:
                        count_product = 0.5 * counts[i] * counts[j]
                    else:
                        count_product = counts[i] * counts[j]

                    # This is the index of the spectrum. It is given by enumerating the
                    # elements of an upper triangular matrix from left to right and top
                    # to bottom.
                    m = int(j + i * n_elem - i * (i + 1) / 2)
                    start = m * n
                    end = (m + 1) * n
                    y_normed = (k2[start:end] * V) / (count_product * 4 * np.pi)
                    k2[start:end] = y_normed

        # Reshape the output if non-flattened descriptor is requested
        if return_descriptor and not self.flatten:
            k2_nonflat = np.zeros((n_elem, n_elem, n), dtype=np.float64)
            for i in range(n_elem):
                for j in range(n_elem):
                    if j < i:
                        continue
                    m = int(j + i * n_elem - i * (i + 1) / 2)
                    start = m * n
                    end = (m + 1) * n
                    k2_nonflat[i, j] = k2[start:end]
            k2 = k2_nonflat

        # Convert to the final output precision.
        if self.dtype == "float32":
            k2 = k2.astype(self.dtype)
            k2_d = k2_d.astype(self.dtype)

        return (k2, k2_d)

    def _get_k3(self, system, return_descriptor, return_derivatives):
        """Calculates the third order term and/or its derivatives with
        regard to atomic positions.

        Returns:
            1D or 4D ndarray:   K2 values. If flatten=True, returns a 1D array
                                and if flatten=False returns a 4D array.
                                If return_descriptor=False, returns an array of
                                shape (0).
            3D ndarray:         K2 derivatives. If return_derivatives=False,
                                returns an array of shape (0,0,0).
        """
        grid = self.k3["grid"]
        start = grid["min"]
        stop = grid["max"]
        n = grid["n"]
        sigma = grid["sigma"]

        # Determine the weighting function and possible radial cutoff
        r_cut = None
        weighting = self.k3.get("weighting")
        parameters = {}
        if weighting is not None:
            weighting_function = weighting["function"]
            if weighting_function == "exp":
                threshold = weighting["threshold"]
                r_cut = weighting.get("r_cut")
                scale = weighting.get("scale")
                # If we want to limit the triplets to a distance r_cut, we need
                # to allow x=2*r_cut in the case of k=3.
                if scale is not None and r_cut is None:
                    r_cut = -0.5 * math.log(threshold) / scale
                elif scale is None and r_cut is not None:
                    scale = -0.5 * math.log(threshold) / r_cut
                parameters = {b"scale": scale, b"threshold": threshold}
            if weighting_function == "smooth_cutoff":
                try:
                    sharpness = weighting["sharpness"]
                except Exception:
                    sharpness = 2
                r_cut = weighting["r_cut"]
                parameters = {b"sharpness": sharpness, b"cutoff": r_cut}
        else:
            weighting_function = "unity"

        # Determine the geometry function
        geom_func_name = self.k3["geometry"]["function"]

        # If needed, create the extended system
        if self.periodic:
            centers = system.get_positions()
            ext_system, cell_indices = dscribe.utils.geometry.get_extended_system(
                system, r_cut, centers, return_cell_indices=True
            )
            ext_system = System.from_atoms(ext_system)
        else:
            ext_system = System.from_atoms(system)
            cell_indices = np.zeros((len(system), 3), dtype=int)

        cmbtr = MBTRWrapper(
            self.atomic_number_to_index, self._interaction_limit, cell_indices
        )

        n_atoms = len(ext_system)
        if r_cut is not None:
            dmat = ext_system.get_distance_matrix_within_radius(r_cut)
            adj_list = dscribe.utils.geometry.get_adjacency_list(dmat)
            dmat_dense = np.full(
                (n_atoms, n_atoms), sys.float_info.max
            )  # The non-neighbor values are treated as "infinitely far".
            dmat_dense[dmat.col, dmat.row] = dmat.data
        # If no weighting is used, the full distance matrix is calculated
        else:
            dmat_dense = ext_system.get_distance_matrix()
            adj_list = np.tile(np.arange(n_atoms), (n_atoms, 1))

        n_elem = self.n_elements
        n_features = int((n_elem * n_elem * (n_elem + 1) / 2) * n)

        if return_descriptor:
            k3 = np.zeros((n_features), dtype=np.float64)
        else:
            k3 = np.zeros((0), dtype=np.float64)

        if return_derivatives:
            k3_d = np.zeros((self._interaction_limit, 3, n_features), dtype=np.float64)
        else:
            k3_d = np.zeros((0, 0, 0), dtype=np.float64)

        # Compute the k=3 term and its derivative
        cmbtr.get_k3(
            k3,
            k3_d,
            return_descriptor,
            return_derivatives,
            ext_system.get_atomic_numbers(),
            ext_system.get_positions(),
            dmat_dense,
            adj_list,
            geom_func_name.encode(),
            weighting_function.encode(),
            parameters,
            start,
            stop,
            sigma,
            n,
        )

        # Denormalize if requested
        if not self.normalize_gaussians:
            max_val = 1 / (sigma * math.sqrt(2 * math.pi))
            k3 /= max_val
            k3_d /= max_val

        # Valle-Oganov normalization is calculated separately for each triplet
        # Not implemented for derivatives.
        if self.normalization == "valle_oganov":
            for i in range(n_elem):
                for j in range(n_elem):
                    for k in range(n_elem):
                        if k < i:
                            continue
                        S = self.system
                        n_elements = len(self.species)
                        V = S.cell.volume
                        imap = self.index_to_atomic_number
                        # Calculate the amount of each element for N_A*N_B*N_C term
                        counts = {}
                        for index, number in imap.items():
                            counts[index] = list(S.get_atomic_numbers()).count(number)

                        # This is the index of the spectrum. It is given by enumerating the
                        # elements of a three-dimensional array where for valid elements
                        # k>=i. The enumeration begins from [0, 0, 0], and ends at [n_elem,
                        # n_elem, n_elem], looping the elements in the order j, i, k.
                        m = int(
                            j * n_elem * (n_elem + 1) / 2
                            + k
                            + i * n_elem
                            - i * (i + 1) / 2
                        )
                        start = m * n
                        end = (m + 1) * n
                        count_product = counts[i] * counts[j] * counts[k]
                        y_normed = (k3[start:end] * V) / count_product
                        k3[start:end] = y_normed

        # If non-flattened descriptor is requested, reshape the output
        if return_descriptor and not self.flatten:
            k3_nonflat = np.zeros((n_elem, n_elem, n_elem, n), dtype=np.float64)
            for i in range(n_elem):
                for j in range(n_elem):
                    for k in range(n_elem):
                        if k < i:
                            continue
                        m = int(
                            j * n_elem * (n_elem + 1) / 2
                            + k
                            + i * n_elem
                            - i * (i + 1) / 2
                        )
                        start = m * n
                        end = (m + 1) * n
                        k3_nonflat[i, j, k] = k3[start:end]
            k3 = k3_nonflat

        # Convert to the final output precision.
        if self.dtype == "float32":
            k3 = k3.astype(self.dtype)
            k3_d = k3_d.astype(self.dtype)

        return (k3, k3_d)

    def derivatives(
        self,
        system,
        include=None,
        exclude=None,
        method="auto",
        return_descriptor=True,
        n_jobs=1,
        only_physical_cores=False,
        verbose=False,
    ):
        """Return the descriptor derivatives for the given system.

        Args:
            system (:class:`ase.Atoms` or list of :class:`ase.Atoms`): One or
                many atomic structures.
            include (list): Indices of atoms to compute the derivatives on.
                When calculating descriptor for multiple systems, provide
                either a one-dimensional list that if applied to all systems or
                a two-dimensional list of indices. Cannot be provided together
                with 'exclude'.
            exclude (list): Indices of atoms not to compute the derivatives on.
                When calculating descriptor for multiple systems, provide
                either a one-dimensional list that if applied to all systems or
                a two-dimensional list of indices. Cannot be provided together
                with 'include'.
            return_descriptor (bool): Whether to also calculate the descriptor
                in the same function call. Notice that it typically is faster
                to calculate both in one go.
            n_jobs (int): Number of parallel jobs to instantiate. Parallellizes
                the calculation across samples. Defaults to serial calculation
                with n_jobs=1. If a negative number is given, the number of jobs
                will be calculated with, n_cpus + n_jobs, where n_cpus is the
                amount of CPUs as reported by the OS. With only_physical_cores
                you can control which types of CPUs are counted in n_cpus.
            only_physical_cores (bool): If a negative n_jobs is given,
                determines which types of CPUs are used in calculating the
                number of jobs. If set to False (default), also virtual CPUs
                are counted.  If set to True, only physical CPUs are counted.
            verbose(bool): Controls whether to print the progress of each job
                into to the console.

        Returns:
            If return_descriptor is True, returns a tuple, where the first item
            is the derivative array and the second is the descriptor array.
            Otherwise only returns the derivatives array. The derivatives array
            is a either a 3D or 4D array, depending on whether you have
            provided a single or multiple systems. If the output shape for each
            system is the same, a single monolithic numpy array is returned.
            For variable sized output (e.g. differently sized systems,different
            number of included atoms), a regular python list is returned. The
            dimensions are: [(n_systems,) n_atoms, 3, n_features]. The first
            dimension goes over the different systems in case multiple were
            given. The second dimension goes over the included atoms. The order
            is same as the order of atoms in the given system. The third
            dimension goes over the cartesian components, x, y and z. The
            fourth dimension goes over the features in the default order.
        """
        # Validate/determine the appropriate calculation method.
        methods = {"analytical", "auto"}
        if method not in methods:
            raise ValueError(
                "Invalid method specified. Please choose from: {}".format(methods)
            )
        if method == "auto":
            method = "analytical"

        # Check that the derivative calculations are supported for the used
        # MBTR parameters
        supported_normalization = ["none", "n_atoms"]
        if self.normalization not in supported_normalization:
            raise ValueError(
                "Derivatives not implemented for normalization option '{}'. Please choose from: {}".format(
                    self.normalization, supported_normalization
                )
            )

        if self.flatten == False:
            raise ValueError("Derivatives not implemented for flatten=False.")

        # Derivatives are not currently implemented for all k3 options
        if self.k3 is not None:
            if self.k3.get("weighting") is not None:
                if self.k3["weighting"]["function"] == "smooth_cutoff":
                    raise ValueError(
                        "Derivatives not implemented for k3 weighting function 'smooth_cutoff'."
                    )

            # "angle" function is not differentiable
            if self.k3["geometry"]["function"] == "angle":
                raise ValueError(
                    "Derivatives not implemented for k3 geometry function 'angle'."
                )

        # Check input validity
        system = [system] if isinstance(system, Atoms) else system
        n_samples = len(system)
        if include is None:
            include = [None] * n_samples
        elif is1d(include, np.integer):
            include = [include] * n_samples
        if exclude is None:
            exclude = [None] * n_samples
        elif is1d(exclude, np.integer):
            exclude = [exclude] * n_samples
        n_inc = len(include)
        if n_inc != n_samples:
            raise ValueError(
                "The given number of includes does not match the given "
                "number of systems."
            )
        n_exc = len(exclude)
        if n_exc != n_samples:
            raise ValueError(
                "The given number of excludes does not match the given "
                "number of systems."
            )

        # Determine the atom indices that are displaced
        indices = []
        for sys, inc, exc in zip(system, include, exclude):
            n_atoms = len(sys)
            indices.append(self._get_indices(n_atoms, inc, exc))

        # Combine input arguments
        inp = list(
            zip(
                system,
                indices,
                [method] * n_samples,
                [return_descriptor] * n_samples,
            )
        )

        # Determine a fixed output size if possible
        n_features = self.get_number_of_features()

        def get_shapes(job):
            n_indices = len(job[1])
            return (n_indices, 3, n_features), (n_features,)

        derivatives_shape, descriptor_shape = get_shapes(inp[0])

        def is_variable(inp):
            for job in inp[1:]:
                i_der_shape, i_desc_shape = get_shapes(job)
                if i_der_shape != derivatives_shape or i_desc_shape != descriptor_shape:
                    return True
            return False

        if is_variable(inp):
            derivatives_shape = None
            descriptor_shape = None

        # Create in parallel
        output = self.derivatives_parallel(
            inp,
            self.derivatives_single,
            n_jobs,
            derivatives_shape,
            descriptor_shape,
            return_descriptor,
            only_physical_cores,
            verbose=verbose,
        )

        return output

    def derivatives_single(
        self,
        system,
        indices,
        method="analytical",
        return_descriptor=True,
    ):
        """Return the derivatives for the given system.

        Args:
            system (:class:`ase.Atoms`): Atomic structure.
            indices (list): Indices of atoms for which the derivatives will be
                computed for.
            method (str): The method for calculating the derivatives. Supports
                'analytical'.
            return_descriptor (bool): Whether to also calculate the descriptor
                in the same function call. This is true by default as it
                typically is faster to calculate both in one go.

        Returns:
            If return_descriptor is True, returns a tuple, where the first item
            is the derivative array and the second is the descriptor array.
            Otherwise only returns the derivatives array. The derivatives array
            is a 3D numpy array. The dimensions are: [n_atoms, 3, n_features].
            The first dimension goes over all the atoms in the system. The
            second dimension goes over the cartesian components, x, y and z.
            The last dimension goes over the features in the default order.
        """

        # Ensuring variables are re-initialized when a new system is introduced
        self.system = system
        self._interaction_limit = len(system)

        # Check that the system does not have elements that are not in the list
        # of atomic numbers
        self.check_atomic_numbers(system.get_atomic_numbers())

        mbtr = {}
        mbtr_d = {}
        if self.k1 is not None:
            k1, k1_d = self._get_k1(system, return_descriptor, True)
            mbtr["k1"] = k1
            mbtr_d["k1"] = k1_d
        if self.k2 is not None:
            k2, k2_d = self._get_k2(system, return_descriptor, True)
            mbtr["k2"] = k2
            mbtr_d["k2"] = k2_d
        if self.k3 is not None:
            k3, k3_d = self._get_k3(system, return_descriptor, True)
            mbtr["k3"] = k3
            mbtr_d["k3"] = k3_d

        # Handle normalization
        if self.normalization == "l2_each":
            # Normalization factor is a function of atomic positions.
            # Not implemented
            pass
        elif self.normalization == "n_atoms":
            n_atoms = len(self.system)
            if self.flatten is True:
                for key, value in mbtr.items():
                    mbtr[key] = value / n_atoms
                    mbtr_d[key] /= n_atoms
            else:
                for key, value in mbtr.items():
                    mbtr[key] = value / n_atoms
                    mbtr_d[key] /= n_atoms

        keys = sorted(mbtr.keys())
        if len(keys) > 1:
            mbtr = np.concatenate([mbtr[key] for key in keys], axis=0)
            mbtr_d = np.concatenate([mbtr_d[key] for key in keys], axis=2)
        else:
            mbtr = mbtr[keys[0]]
            mbtr_d = mbtr_d[keys[0]]

        # For now, the derivatives are calculated with regard to all atomic
        # positions. The desired indices are extracted here at the end.
        if len(indices) < len(self.system):
            mbtr_d = mbtr_d[indices]

        if self.sparse:
            mbtr = sparse.COO.from_numpy(mbtr)
            mbtr_d = sparse.COO.from_numpy(mbtr_d)

        if return_descriptor:
            return (mbtr_d, mbtr)
        return mbtr_d
