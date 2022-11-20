#
# Interface for discretisation
#
import pybamm
import numpy as np
from collections import defaultdict


def has_bc_of_form(symbol, side, bcs, form):

    if symbol in bcs:
        if bcs[symbol][side][1] == form:
            return True
        else:
            return False

    else:
        return False


class Discretisation(object):
    """The discretisation class, with methods to process a model and replace
    Spatial Operators with Matrices and Variables with StateVectors

    Parameters
    ----------
    mesh : pybamm.Mesh
            contains all submeshes to be used on each domain
    spatial_methods : dict
            a dictionary of the spatial methods to be used on each
            domain. The keys correspond to the model domains and the
            values to the spatial method.
    """

    def __init__(self, mesh=None, spatial_methods=None):
        self._mesh = mesh
        if mesh is None:
            self._spatial_methods = {}
        else:
            # Unpack macroscale to the constituent subdomains
            if "macroscale" in spatial_methods.keys():
                method = spatial_methods["macroscale"]
                spatial_methods["negative electrode"] = method
                spatial_methods["separator"] = method
                spatial_methods["positive electrode"] = method

            self._spatial_methods = spatial_methods
            for domain, method in self._spatial_methods.items():
                method.build(mesh)
                # Check zero-dimensional methods are only applied to zero-dimensional
                # meshes
                if isinstance(method, pybamm.ZeroDimensionalSpatialMethod):
                    if not isinstance(mesh[domain], pybamm.SubMesh0D):
                        raise pybamm.DiscretisationError(
                            "Zero-dimensional spatial method for the "
                            "{} domain requires a zero-dimensional submesh".format(
                                domain
                            )
                        )

        self._bcs = {}
        self.y_slices = {}
        self._discretised_symbols = {}
        self.external_variables = {}

    @property
    def mesh(self):
        return self._mesh

    @property
    def y_slices(self):
        return self._y_slices

    @y_slices.setter
    def y_slices(self, value):
        if not isinstance(value, dict):
            raise TypeError("""y_slices should be dict, not {}""".format(type(value)))

        self._y_slices = value

    @property
    def spatial_methods(self):
        return self._spatial_methods

    @property
    def bcs(self):
        return self._bcs

    @bcs.setter
    def bcs(self, value):
        self._bcs = value
        # reset discretised_symbols
        self._discretised_symbols = {}

    def copy_with_discretised_symbols(self):
        """
        Return a copy of the discretisation
        """
        copy = Discretisation(self.mesh, self.spatial_methods)
        copy.bcs = self.bcs.copy()
        copy.y_slices = self.y_slices.copy()
        copy._discretised_symbols = self._discretised_symbols.copy()
        copy.external_variables = self.external_variables.copy()
        return copy

    def process_model(
        self,
        model,
        inplace=True,
        check_model=True,
        remove_independent_variables_from_rhs=True,
    ):
        """Discretise a model.
        Currently inplace, could be changed to return a new model.

        Parameters
        ----------
        model : :class:`pybamm.BaseModel`
            Model to dicretise. Must have attributes rhs, initial_conditions and
            boundary_conditions (all dicts of {variable: equation})
        inplace : bool, optional
            If True, discretise the model in place. Otherwise, return a new
            discretised model. Default is True.
        check_model : bool, optional
            If True, model checks are performed after discretisation. For large
            systems these checks can be slow, so can be skipped by setting this
            option to False. When developing, testing or debugging it is recommended
            to leave this option as True as it may help to identify any errors.
            Default is True.
        remove_independent_variables_from_rhs : bool, optional
            If True, model checks to see whether any variables from the RHS are used
            in any other equation. If a variable meets all of the following criteria
            (not used anywhere in the model, len(rhs)>1), then the variable
            is moved to be explicitly integrated when called by the solution object.
            Default is True.

        Returns
        -------
        model_disc : :class:`pybamm.BaseModel`
            The discretised model. Note that if ``inplace`` is True, model will
            have also been discretised in place so model == model_disc. If
            ``inplace`` is False, model != model_disc

        Raises
        ------
        :class:`pybamm.ModelError`
            If an empty model is passed (`model.rhs = {}` and `model.algebraic = {}` and
            `model.variables = {}`)

        """
        if model.is_discretised is True:
            raise pybamm.ModelError(
                "Cannot re-discretise a model. "
                "Set 'inplace=False' when first discretising a model to then be able "
                "to discretise it more times (e.g. for convergence studies)."
            )

        pybamm.logger.info("Start discretising {}".format(model.name))

        # Make sure model isn't empty
        if (
            len(model.rhs) == 0
            and len(model.algebraic) == 0
            and len(model.variables) == 0
        ):
            raise pybamm.ModelError("Cannot discretise empty model")
        # Check well-posedness to avoid obscure errors
        model.check_well_posedness()

        # Prepare discretisation
        # set variables (we require the full variable not just id)

        # Search Equations for Independence
        # if remove_independent_variables_from_rhs:
        #     model = self.remove_independent_variables_from_rhs(model)
        variables = list(model.rhs.keys()) + list(model.algebraic.keys())
        # Find those RHS's that are constant
        if self.spatial_methods == {} and any(var.domain != [] for var in variables):
            for var in variables:
                if var.domain != []:
                    raise pybamm.DiscretisationError(
                        "Spatial method has not been given "
                        "for variable {} with domain {}".format(var.name, var.domain)
                    )

        # Set the y split for variables
        pybamm.logger.verbose("Set variable slices for {}".format(model.name))
        self.set_variable_slices(variables)

        # now add extrapolated external variables to the boundary conditions
        # if required by the spatial method
        self._preprocess_external_variables(model)
        self.set_external_variables(model)

        # set boundary conditions (only need key ids for boundary_conditions)
        pybamm.logger.verbose(
            "Discretise boundary conditions for {}".format(model.name)
        )
        self._bcs = self.process_boundary_conditions(model)
        pybamm.logger.verbose(
            "Set internal boundary conditions for {}".format(model.name)
        )
        self.set_internal_boundary_conditions(model)

        # Keep a record of y_slices in the model
        y_slices = self.y_slices
        # Keep a record of the bounds in the model
        bounds = self.bounds

        bcs = self.bcs

        pybamm.logger.verbose("Discretise initial conditions for {}".format(model.name))
        initial_conditions = self.process_dict(model.initial_conditions)
        initial_conditions = {
            k: (v - k.reference) / k.scale for k, v in initial_conditions.items()
        }

        # Process parabolic and elliptic equations
        pybamm.logger.verbose("Discretise model equations for {}".format(model.name))
        rhs = self.process_dict(model.rhs)
        rhs = {k: v / k.scale for k, v in rhs.items()}
        algebraic = self.process_dict(model.algebraic)
        algebraic = {k: v / k.scale for k, v in algebraic.items()}

        # Process events
        events = []
        pybamm.logger.verbose("Discretise events for {}".format(model.name))
        for event in model.events:
            pybamm.logger.debug("Discretise event '{}'".format(event.name))
            event = pybamm.Event(
                event.name, self.process_symbol(event.expression), event.event_type
            )
            events.append(event)

        # Set external variables
        external_variables = [
            self.process_symbol(var) for var in model.external_variables
        ]

        # Process length scales
        length_scales = {}
        for domain, scale in model.length_scales.items():
            scale = self.process_symbol(scale)
            if isinstance(scale, pybamm.Array):
                # Convert possible arrays of length 1 to scalars
                scale = pybamm.Scalar(float(scale.evaluate()))
            length_scales[domain] = scale

        discretised_equations = pybamm._DiscretisedEquations(
            self,
            rhs,
            algebraic,
            initial_conditions,
            bcs,
            model.variables,
            events,
            external_variables,
            model.timescale,
            length_scales,
            y_slices=y_slices,
            bounds=bounds,
        )

        # inplace vs not inplace
        if inplace:
            model_disc = model
            model_disc._equations = discretised_equations
        else:
            # create a copy of the original model
            model_disc = model.new_copy(equations=discretised_equations)

        # Check that resulting model makes sense
        if check_model:
            pybamm.logger.verbose("Performing model checks for {}".format(model.name))
            self.check_model(model_disc)

        pybamm.logger.info("Finish discretising {}".format(model.name))

        return model_disc

    def set_variable_slices(self, variables):
        """
        Sets the slicing for variables.

        Parameters
        ----------
        variables : iterable of :class:`pybamm.Variables`
            The variables for which to set slices
        """
        # Set up y_slices and bounds
        y_slices = defaultdict(list)
        start = 0
        end = 0
        lower_bounds = []
        upper_bounds = []
        # Iterate through unpacked variables, adding appropriate slices to y_slices
        for variable in variables:
            # Add up the size of all the domains in variable.domain
            if isinstance(variable, pybamm.ConcatenationVariable):
                start_ = start
                spatial_method = self.spatial_methods[variable.domain[0]]
                children = variable.children
                meshes = {}
                for child in children:
                    meshes[child] = [spatial_method.mesh[dom] for dom in child.domain]
                sec_points = spatial_method._get_auxiliary_domain_repeats(
                    variable.domains
                )
                for i in range(sec_points):
                    for child, mesh in meshes.items():
                        for domain_mesh in mesh:
                            end += domain_mesh.npts_for_broadcast_to_nodes
                        # Add to slices
                        y_slices[child].append(slice(start_, end))
                        # Increment start_
                        start_ = end
            else:
                end += self._get_variable_size(variable)

            # Add to slices
            y_slices[variable].append(slice(start, end))
            # Add to bounds
            lower_bounds.extend([variable.bounds[0]] * (end - start))
            upper_bounds.extend([variable.bounds[1]] * (end - start))
            # Increment start
            start = end

        # Convert y_slices back to normal dictionary
        self.y_slices = dict(y_slices)

        # Also keep a record of bounds
        self.bounds = (np.array(lower_bounds), np.array(upper_bounds))

        # reset discretised_symbols
        self._discretised_symbols = {}

    def _get_variable_size(self, variable):
        """Helper function to determine what size a variable should be"""
        # If domain is empty then variable has size 1
        if variable.domain == []:
            return 1
        else:
            size = 0
            spatial_method = self.spatial_methods[variable.domain[0]]
            repeats = spatial_method._get_auxiliary_domain_repeats(variable.domains)
            for dom in variable.domain:
                size += spatial_method.mesh[dom].npts_for_broadcast_to_nodes * repeats
            return size

    def _preprocess_external_variables(self, model):
        """
        A method to preprocess external variables so that they are
        compatible with the spatial method. For example, in finite
        volume, the user will supply a vector of values valid on the
        cell centres. However, for model processing, we also require
        the boundary edge fluxes. Therefore, we extrapolate and add
        the boundary fluxes to the boundary conditions, which are
        employed in generating the grad and div matrices.
        The processing is delegated to spatial methods as
        the preprocessing required for finite volume and finite
        element will be different.
        """

        for var in model.external_variables:
            if var.domain != []:
                new_bcs = self.spatial_methods[
                    var.domain[0]
                ].preprocess_external_variables(var)

                model.boundary_conditions.update(new_bcs)

    def set_external_variables(self, model):
        """
        Add external variables to the list of variables to account for, being careful
        about concatenations
        """
        for var in model.external_variables:
            # Find the name in the model variables
            # Look up dictionary key based on value
            try:
                idx = list(model.variables.values()).index(var)
            except ValueError:
                raise ValueError(
                    """
                    Variable {} must be in the model.variables dictionary to be set
                    as an external variable
                    """.format(
                        var
                    )
                )
            name = list(model.variables.keys())[idx]
            if isinstance(var, pybamm.Variable):
                # No need to keep track of the parent
                self.external_variables[(name, None)] = var
            elif isinstance(var, pybamm.ConcatenationVariable):
                start = 0
                end = 0
                for child in var.children:
                    dom = child.domain[0]
                    if (
                        self.spatial_methods[dom]._get_auxiliary_domain_repeats(
                            child.domains
                        )
                        > 1
                    ):
                        raise NotImplementedError(
                            "Cannot create 2D external variable with concatenations"
                        )
                    end += self._get_variable_size(child)
                    # Keep a record of the parent
                    self.external_variables[(name, (var, start, end))] = child
                    start = end

    def set_internal_boundary_conditions(self, model):
        """
        A method to set the internal boundary conditions for the submodel.
        These are required to properly calculate the gradient.
        Note: this method modifies the state of self.boundary_conditions.
        """

        def boundary_gradient(left_symbol, right_symbol):

            pybamm.logger.debug(
                "Calculate boundary gradient ({} and {})".format(
                    left_symbol, right_symbol
                )
            )
            left_domain = left_symbol.domain[0]
            right_domain = right_symbol.domain[0]

            left_mesh = self.spatial_methods[left_domain].mesh[left_domain]
            right_mesh = self.spatial_methods[right_domain].mesh[right_domain]

            left_symbol_disc = self.process_symbol(left_symbol)
            right_symbol_disc = self.process_symbol(right_symbol)

            return self.spatial_methods[left_domain].internal_neumann_condition(
                left_symbol_disc, right_symbol_disc, left_mesh, right_mesh
            )

        bc_keys = list(self.bcs.keys())

        internal_bcs = {}
        for var in model.boundary_conditions.keys():
            if isinstance(var, pybamm.Concatenation):
                children = var.orphans

                first_child = children[0]
                next_child = children[1]

                lbc = self.bcs[var]["left"]
                rbc = (boundary_gradient(first_child, next_child), "Neumann")

                if first_child not in bc_keys:
                    internal_bcs.update({first_child: {"left": lbc, "right": rbc}})

                for current_child, next_child in zip(children[1:-1], children[2:]):
                    lbc = rbc
                    rbc = (boundary_gradient(current_child, next_child), "Neumann")
                    if current_child not in bc_keys:
                        internal_bcs.update(
                            {current_child: {"left": lbc, "right": rbc}}
                        )

                lbc = rbc
                rbc = self.bcs[var]["right"]
                if children[-1] not in bc_keys:
                    internal_bcs.update({children[-1]: {"left": lbc, "right": rbc}})

        self.bcs.update(internal_bcs)

    def process_boundary_conditions(self, model):
        """Discretise model boundary_conditions, also converting keys to ids

        Parameters
        ----------
        model : :class:`pybamm.BaseModel`
            Model to dicretise. Must have attributes rhs, initial_conditions and
            boundary_conditions (all dicts of {variable: equation})

        Returns
        -------
        dict
            Dictionary of processed boundary conditions

        """

        processed_bcs = {}

        # process and set pybamm.variables first incase required
        # in discrisation of other boundary conditions
        for key, bcs in model.boundary_conditions.items():
            processed_bcs[key] = {}

            # check if the boundary condition at the origin for sphere domains is other
            # than no flux
            if key not in model.external_variables:
                for subdomain in key.domain:
                    if self.mesh[subdomain].coord_sys == "spherical polar":
                        if bcs["left"][0].value != 0 or bcs["left"][1] != "Neumann":
                            raise pybamm.ModelError(
                                "Boundary condition at r = 0 must be a homogeneous "
                                "Neumann condition for {} coordinates".format(
                                    self.mesh[subdomain].coord_sys
                                )
                            )

            # Handle any boundary conditions applied on the tabs
            if any("tab" in side for side in list(bcs.keys())):
                bcs = self.check_tab_conditions(key, bcs)

            # Process boundary conditions
            for side, bc in bcs.items():
                eqn, typ = bc
                pybamm.logger.debug("Discretise {} ({} bc)".format(key, side))
                processed_eqn = self.process_symbol(eqn)
                processed_bcs[key][side] = (processed_eqn, typ)

        return processed_bcs

    def check_tab_conditions(self, symbol, bcs):
        """
        Check any boundary conditions applied on "negative tab", "positive tab"
        and "no tab". For 1D current collector meshes, these conditions are
        converted into boundary conditions on "left" (tab at z=0) or "right"
        (tab at z=l_z) depending on the tab location stored in the mesh. For 2D
        current collector meshes, the boundary conditions can be applied on the
        tabs directly.

        Parameters
        ----------
        symbol : :class:`pybamm.expression_tree.symbol.Symbol`
            The symbol on which the boundary conditions are applied.
        bcs : dict
            The dictionary of boundary conditions (a dict of {side: equation}).

        Returns
        -------
        dict
            The dictionary of boundary conditions, with the keys changed to
            "left" and "right" where necessary.

        """
        # Check symbol domain
        domain = symbol.domain[0]
        mesh = self.mesh[domain]

        if domain != "current collector":
            raise pybamm.ModelError(
                """Boundary conditions can only be applied on the tabs in the domain
            'current collector', but {} has domain {}""".format(
                    symbol, domain
                )
            )
        # Replace keys with "left" and "right" as appropriate for 1D meshes
        if isinstance(mesh, pybamm.SubMesh1D):
            # send boundary conditions applied on the tabs to "left" or "right"
            # depending on the tab location stored in the mesh
            for tab in ["negative tab", "positive tab"]:
                if any(tab in side for side in list(bcs.keys())):
                    bcs[mesh.tabs[tab]] = bcs.pop(tab)
            # if there was a tab at either end, then the boundary conditions
            # have now been set on "left" and "right" as required by the spatial
            # method, so there is no need to further modify the bcs dict
            if all(side in list(bcs.keys()) for side in ["left", "right"]):
                pass
            # if both tabs are located at z=0 then the "right" boundary condition
            # (at z=1) is the condition for "no tab"
            elif "left" in list(bcs.keys()):
                bcs["right"] = bcs.pop("no tab")
            # else if both tabs are located at z=1, the "left" boundary condition
            # (at z=0) is the condition for "no tab"
            else:
                bcs["left"] = bcs.pop("no tab")

        return bcs

    def process_dict(self, var_eqn_dict):
        """Discretise a dictionary of {variable: equation}, broadcasting if necessary
        (can be model.rhs, model.algebraic, model.initial_conditions or
        model.variables).

        Parameters
        ----------
        var_eqn_dict : dict
            Equations ({variable: equation} dict) to dicretise
            (can be model.rhs, model.algebraic, model.initial_conditions or
            model.variables)
        ics : bool, optional
            Whether the equations are initial conditions. If True, the equations are
            scaled by the reference value of the variable, if given

        Returns
        -------
        new_var_eqn_dict : dict
            Discretised equations

        """
        new_var_eqn_dict = {}
        for eqn_key, eqn in var_eqn_dict.items():
            # Broadcast if the equation evaluates to a number (e.g. Scalar)
            if np.prod(eqn.shape_for_testing) == 1 and not isinstance(eqn_key, str):
                if eqn_key.domain == []:
                    eqn = eqn * pybamm.Vector([1])
                else:
                    eqn = pybamm.FullBroadcast(eqn, broadcast_domains=eqn_key.domains)

            pybamm.logger.debug("Discretise {!r}".format(eqn_key))
            processed_eqn = self.process_symbol(eqn)

            new_var_eqn_dict[eqn_key] = processed_eqn
        return new_var_eqn_dict

    def process_symbol(self, symbol):
        """Discretise operators in model equations.
        If a symbol has already been discretised, the stored value is returned.

        Parameters
        ----------
        symbol : :class:`pybamm.expression_tree.symbol.Symbol`
            Symbol to discretise

        Returns
        -------
        :class:`pybamm.expression_tree.symbol.Symbol`
            Discretised symbol

        """
        try:
            return self._discretised_symbols[symbol]
        except KeyError:
            discretised_symbol = self._process_symbol(symbol)
            self._discretised_symbols[symbol] = discretised_symbol
            discretised_symbol.test_shape()

            # Assign mesh as an attribute to the processed variable
            if symbol.domain != []:
                discretised_symbol.mesh = self.mesh[symbol.domain]
            else:
                discretised_symbol.mesh = None

            # Assign secondary mesh
            if symbol.domains["secondary"] != []:
                discretised_symbol.secondary_mesh = self.mesh[
                    symbol.domains["secondary"]
                ]
            else:
                discretised_symbol.secondary_mesh = None
            return discretised_symbol

    def _process_symbol(self, symbol):
        """See :meth:`Discretisation.process_symbol()`."""

        if symbol.domain != []:
            spatial_method = self.spatial_methods[symbol.domain[0]]
            # If boundary conditions are provided, need to check for BCs on tabs
            if self.bcs:
                key_id = list(self.bcs.keys())[0]
                if any("tab" in side for side in list(self.bcs[key_id].keys())):
                    self.bcs[key_id] = self.check_tab_conditions(
                        symbol, self.bcs[key_id]
                    )

        if isinstance(symbol, pybamm.BinaryOperator):
            # Pre-process children
            left, right = symbol.children
            disc_left = self.process_symbol(left)
            disc_right = self.process_symbol(right)
            if symbol.domain == []:
                return pybamm.simplify_if_constant(
                    symbol._binary_new_copy(disc_left, disc_right)
                )
            else:
                return spatial_method.process_binary_operators(
                    symbol, left, right, disc_left, disc_right
                )
        elif isinstance(symbol, pybamm._BaseAverage):
            # Create a new Integral operator and process it
            child = symbol.orphans[0]
            if isinstance(symbol, pybamm.SizeAverage):
                R = symbol.integration_variable[0]
                f_a_dist = symbol.f_a_dist
                # take average using Integral and distribution f_a_dist
                average = pybamm.Integral(f_a_dist * child, R) / pybamm.Integral(
                    f_a_dist, R
                )
            else:
                x = symbol.integration_variable
                v = pybamm.ones_like(child)
                average = pybamm.Integral(child, x) / pybamm.Integral(v, x)
            return self.process_symbol(average)

        elif isinstance(symbol, pybamm.UnaryOperator):
            child = symbol.child

            disc_child = self.process_symbol(child)
            if child.domain != []:
                child_spatial_method = self.spatial_methods[child.domain[0]]

            if isinstance(symbol, pybamm.Gradient):
                return child_spatial_method.gradient(child, disc_child, self.bcs)

            elif isinstance(symbol, pybamm.Divergence):
                return child_spatial_method.divergence(child, disc_child, self.bcs)

            elif isinstance(symbol, pybamm.Laplacian):
                return child_spatial_method.laplacian(child, disc_child, self.bcs)

            elif isinstance(symbol, pybamm.GradientSquared):
                return child_spatial_method.gradient_squared(
                    child, disc_child, self.bcs
                )

            elif isinstance(symbol, pybamm.Mass):
                return child_spatial_method.mass_matrix(child, self.bcs)

            elif isinstance(symbol, pybamm.BoundaryMass):
                return child_spatial_method.boundary_mass_matrix(child, self.bcs)

            elif isinstance(symbol, pybamm.IndefiniteIntegral):
                return child_spatial_method.indefinite_integral(
                    child, disc_child, "forward"
                )
            elif isinstance(symbol, pybamm.BackwardIndefiniteIntegral):
                return child_spatial_method.indefinite_integral(
                    child, disc_child, "backward"
                )

            elif isinstance(symbol, pybamm.Integral):
                integral_spatial_method = self.spatial_methods[
                    symbol.integration_variable[0].domain[0]
                ]
                out = integral_spatial_method.integral(
                    child, disc_child, symbol._integration_dimension
                )
                out.copy_domains(symbol)
                return out

            elif isinstance(symbol, pybamm.DefiniteIntegralVector):
                return child_spatial_method.definite_integral_matrix(
                    child, vector_type=symbol.vector_type
                )

            elif isinstance(symbol, pybamm.BoundaryIntegral):
                return child_spatial_method.boundary_integral(
                    child, disc_child, symbol.region
                )

            elif isinstance(symbol, pybamm.Broadcast):
                # Broadcast new_child to the domain specified by symbol.domain
                # Different discretisations may broadcast differently
                return spatial_method.broadcast(
                    disc_child, symbol.domains, symbol.broadcast_type
                )

            elif isinstance(symbol, pybamm.DeltaFunction):
                return spatial_method.delta_function(symbol, disc_child)

            elif isinstance(symbol, pybamm.BoundaryOperator):
                # if boundary operator applied on "negative tab" or
                # "positive tab" *and* the mesh is 1D then change side to
                # "left" or "right" as appropriate
                if symbol.side in ["negative tab", "positive tab"]:
                    mesh = self.mesh[symbol.children[0].domain[0]]
                    if isinstance(mesh, pybamm.SubMesh1D):
                        symbol.side = mesh.tabs[symbol.side]
                return child_spatial_method.boundary_value_or_flux(
                    symbol, disc_child, self.bcs
                )
            elif isinstance(symbol, pybamm.UpwindDownwind):
                direction = symbol.name  # upwind or downwind
                return spatial_method.upwind_or_downwind(
                    child, disc_child, self.bcs, direction
                )
            elif isinstance(symbol, pybamm.NotConstant):
                # After discretisation, we can make the symbol constant
                return disc_child
            else:
                return symbol._unary_new_copy(disc_child)

        elif isinstance(symbol, pybamm.Function):
            disc_children = [self.process_symbol(child) for child in symbol.children]
            return symbol._function_new_copy(disc_children)

        elif isinstance(symbol, pybamm.VariableDot):
            # Add symbol's reference and multiply by the symbol's scale
            # so that the state vector is of order 1
            return symbol.reference + symbol.scale * pybamm.StateVectorDot(
                *self.y_slices[symbol.get_variable()],
                domains=symbol.domains,
            )

        elif isinstance(symbol, pybamm.Variable):
            # Check if variable is a standard variable or an external variable
            if any(symbol == var for var in self.external_variables.values()):
                # Look up dictionary key based on value
                idx = list(self.external_variables.values()).index(symbol)
                name, parent_and_slice = list(self.external_variables.keys())[idx]
                if parent_and_slice is None:
                    # Variable didn't come from a concatenation so we can just create a
                    # normal external variable using the symbol's name
                    return pybamm.ExternalVariable(
                        symbol.name,
                        size=self._get_variable_size(symbol),
                        domains=symbol.domains,
                    )
                else:
                    # We have to use a special name since the concatenation doesn't have
                    # a very informative name. Needs improving
                    parent, start, end = parent_and_slice
                    ext = pybamm.ExternalVariable(
                        name,
                        size=self._get_variable_size(parent),
                        domains=parent.domains,
                    )
                    out = pybamm.Index(ext, slice(start, end))
                    out.copy_domains(symbol)
                    return out
            else:
                # add a try except block for a more informative error if a variable
                # can't be found. This should usually be caught earlier by
                # model.check_well_posedness, but won't be if debug_mode is False
                try:
                    y_slices = self.y_slices[symbol]
                except KeyError:
                    raise pybamm.ModelError(
                        """
                        No key set for variable '{}'. Make sure it is included in either
                        model.rhs, model.algebraic, or model.external_variables in an
                        unmodified form (e.g. not Broadcasted)
                        """.format(
                            symbol.name
                        )
                    )
                # Add symbol's reference and multiply by the symbol's scale
                # so that the state vector is of order 1
                return symbol.reference + symbol.scale * pybamm.StateVector(
                    *y_slices, domains=symbol.domains
                )

        elif isinstance(symbol, pybamm.SpatialVariable):
            return spatial_method.spatial_variable(symbol)

        elif isinstance(symbol, pybamm.ConcatenationVariable):
            # create new children without scale and reference
            # the scale and reference will be applied to the concatenation instead
            new_children = []
            for child in symbol.children:
                child = child.create_copy()
                child._scale = 1
                child._reference = 0
                child.set_id()
                new_children.append(self.process_symbol(child))
            new_symbol = spatial_method.concatenation(new_children)
            # apply scale to the whole concatenation
            return symbol.reference + symbol.scale * new_symbol

        elif isinstance(symbol, pybamm.Concatenation):
            new_children = [self.process_symbol(child) for child in symbol.children]
            new_symbol = spatial_method.concatenation(new_children)
            return new_symbol

        elif isinstance(symbol, pybamm.InputParameter):
            if symbol.domain != []:
                expected_size = self._get_variable_size(symbol)
            else:
                expected_size = None
            if symbol._expected_size is None:
                symbol._expected_size = expected_size
            return symbol.create_copy()
        else:
            # Backup option: return the object
            return symbol

    def concatenate(self, *symbols, sparse=False):
        if sparse:
            return pybamm.SparseStack(*symbols)
        else:
            return pybamm.numpy_concatenation(*symbols)

    def _concatenate_in_order(self, var_eqn_dict, check_complete=False, sparse=False):
        """
        Concatenate a dictionary of {variable: equation} using self.y_slices

        The keys/variables in `var_eqn_dict` must be the same as the ids in
        `self.y_slices`.
        The resultant concatenation is ordered according to the ordering of the slice
        values in `self.y_slices`

        Parameters
        ----------
        var_eqn_dict : dict
            Equations ({variable: equation} dict) to dicretise
        check_complete : bool, optional
            Whether to check keys in var_eqn_dict against self.y_slices. Default
            is False
        sparse : bool, optional
            If True the concatenation will be a :class:`pybamm.SparseStack`. If
            False the concatenation will be a :class:`pybamm.NumpyConcatenation`.
            Default is False

        Returns
        -------
        var_eqn_dict : dict
            Discretised right-hand side equations

        """
        # Unpack symbols in variables that are concatenations of variables
        unpacked_variables = []
        slices = []
        for symbol in var_eqn_dict.keys():
            if isinstance(symbol, pybamm.ConcatenationVariable):
                unpacked_variables.extend([symbol] + [var for var in symbol.children])
            else:
                unpacked_variables.append(symbol)
            slices.append(self.y_slices[symbol][0])

        if check_complete:
            # Check keys from the given var_eqn_dict against self.y_slices
            unpacked_variables_set = set(unpacked_variables)
            external_vars = set(self.external_variables.values())
            for var in self.external_variables.values():
                child_vars = set(var.children)
                external_vars = external_vars.union(child_vars)
            y_slices_with_external_removed = set(self.y_slices.keys()).difference(
                external_vars
            )
            if unpacked_variables_set != y_slices_with_external_removed:
                given_variable_names = [v.name for v in var_eqn_dict.keys()]
                raise pybamm.ModelError(
                    "Initial conditions are insufficient. Only "
                    "provided for {} ".format(given_variable_names)
                )

        equations = list(var_eqn_dict.values())

        # sort equations according to slices
        sorted_equations = [eq for _, eq in sorted(zip(slices, equations))]

        return self.concatenate(*sorted_equations, sparse=sparse)

    def check_model(self, model):
        """Perform some basic checks to make sure the discretised model makes sense."""
        self.check_initial_conditions(model)
        self.check_variables(model)

    def check_initial_conditions(self, model):

        # Check initial conditions are a numpy array
        # Individual
        for var, eqn in model.initial_conditions.items():
            ic_eval = eqn.evaluate(t=0, inputs="shape test")
            if not isinstance(ic_eval, np.ndarray):
                raise pybamm.ModelError(
                    "initial conditions must be numpy array after discretisation but "
                    "they are {} for variable '{}'.".format(type(ic_eval), var)
                )

            # Check that the initial condition is within the bounds
            # Skip this check if there are input parameters in the initial conditions
            bounds = var.bounds
            if not eqn.has_symbol_of_classes(pybamm.InputParameter) and not (
                all(bounds[0] <= ic_eval) and all(ic_eval <= bounds[1])
            ):
                raise pybamm.ModelError(
                    "initial condition is outside of variable bounds "
                    "{} for variable '{}'.".format(bounds, var)
                )

        # Check initial conditions and model equations have the same shape
        # Individual
        for var in model.rhs.keys():
            if model.rhs[var].shape != model.initial_conditions[var].shape:
                raise pybamm.ModelError(
                    "rhs and initial conditions must have the same shape after "
                    "discretisation but rhs.shape = "
                    "{} and initial_conditions.shape = {} for variable '{}'.".format(
                        model.rhs[var].shape, model.initial_conditions[var].shape, var
                    )
                )
        for var in model.algebraic.keys():
            if model.algebraic[var].shape != model.initial_conditions[var].shape:
                raise pybamm.ModelError(
                    "algebraic and initial conditions must have the same shape after "
                    "discretisation but algebraic.shape = "
                    "{} and initial_conditions.shape = {} for variable '{}'.".format(
                        model.algebraic[var].shape,
                        model.initial_conditions[var].shape,
                        var,
                    )
                )

    def check_variables(self, model):
        """
        Check variables in variable list against rhs
        Be lenient with size check if the variable in model.variables is broadcasted, or
        a concatenation
        (if broadcasted, variable is a multiplication with a vector of ones)
        """
        for rhs_var in model.rhs.keys():
            if rhs_var.name in model.variables.keys():
                var = model.variables[rhs_var.name]

                different_shapes = not np.array_equal(
                    model.rhs[rhs_var].shape, var.shape
                )

                not_concatenation = not isinstance(var, pybamm.Concatenation)

                not_mult_by_one_vec = not (
                    isinstance(
                        var, (pybamm.Multiplication, pybamm.MatrixMultiplication)
                    )
                    and (
                        pybamm.is_matrix_one(var.left)
                        or pybamm.is_matrix_one(var.right)
                    )
                )

                if different_shapes and not_concatenation and not_mult_by_one_vec:
                    raise pybamm.ModelError(
                        "variable and its eqn must have the same shape after "
                        "discretisation but variable.shape = "
                        "{} and rhs.shape = {} for variable '{}'. ".format(
                            var.shape, model.rhs[rhs_var].shape, var
                        )
                    )

    def is_variable_independent(self, var, all_vars_in_eqns):
        pybamm.logger.verbose("Removing independent blocks.")
        if not isinstance(var, pybamm.Variable):
            return False

        this_var_is_independent = not (var.name in all_vars_in_eqns)
        not_in_y_slices = not (var in list(self.y_slices.keys()))
        not_in_discretised = not (var in list(self._discretised_symbols.keys()))
        is_0D = len(var.domain) == 0
        this_var_is_independent = (
            this_var_is_independent and not_in_y_slices and not_in_discretised and is_0D
        )
        return this_var_is_independent

    def remove_independent_variables_from_rhs(self, model):
        rhs_vars_to_search_over = list(model.rhs.keys())
        unpacker = pybamm.SymbolUnpacker(pybamm.Variable)
        eqns_to_check = (
            list(model.rhs.values())
            + list(model.algebraic.values())
            + [
                x[side][0]
                for x in model.boundary_conditions.values()
                for side in x.keys()
            ]
            # only check children of variables, this will skip the variable itself
            # and catch any other cases
            + [child for var in model.variables.values() for child in var.children]
        )
        all_vars_in_eqns = unpacker.unpack_list_of_symbols(eqns_to_check)
        all_vars_in_eqns = [var.name for var in all_vars_in_eqns]

        for var in rhs_vars_to_search_over:
            this_var_is_independent = self.is_variable_independent(
                var, all_vars_in_eqns
            )
            if this_var_is_independent:
                if len(model.rhs) != 1:
                    pybamm.logger.info("removing variable {} from rhs".format(var))
                    my_initial_condition = model.initial_conditions[var]
                    model.variables[var.name] = pybamm.ExplicitTimeIntegral(
                        model.rhs[var], my_initial_condition
                    )
                    # edge case where a variable appears
                    # in variables twice under different names
                    for key in model.variables:
                        if model.variables[key] == var:
                            model.variables[key] = model.variables[var.name]
                    del model.rhs[var]
                    del model.initial_conditions[var]
                else:
                    break
        return model
