import collections
import datetime as _datetime
from collections import OrderedDict
from typing import Callable, Union, Dict, DefaultDict, Type, Generator

from flytekit import FlyteContext, engine as flytekit_engine, logger
from flytekit.annotated import type_engine
from flytekit.annotated.context_manager import ExecutionState
from flytekit.common import nodes as _nodes, interface as _common_interface
from flytekit.common.exceptions import user as _user_exceptions
from flytekit.common.promise import NodeOutput as _NodeOutput
from flytekit.models import task as _task_model, literals as _literal_models, interface as _interface_models
from flytekit.models.core import workflow as _workflow_model, identifier as _identifier_model


# Dummy SDKNode mimic
class A(object):
    def id(self):
        return "dummy-node"


# This is the least abstract task. It will have access to the loaded Python function
# itself if run locally, so it will always be a Python task.
# This is analogous to the current SdkRunnableTask. Need to analyze the benefits of duplicating the class versus
# adding to it. Also thinking that the relationship to SdkTask should be a has-one relationship rather than an is-one.
# I'm not attached to this class at all, it's just here as a stand-in. Everything in this PR is subject to change.
#
# I think the class layers are IDL -> Model class -> SdkBlah class. While the model and generated-IDL classes
# obviously encapsulate the IDL, the SdkTask/Workflow/Launchplan/Node classes should encapsulate the control plane.
# That is, all the control plane interactions we wish to build should belong there. (I think this is how it's done
# already.)
class Task(object):

    def __init__(self, task_function, interface, metadata: _task_model.TaskMetadata, *args, **kwargs):
        self._task_function = task_function
        self._interface = interface
        self._metadata = metadata

    def _create_and_link_node(self, ctx: FlyteContext, *args, **kwargs):
        used_inputs = set()
        bindings = []

        for k in sorted(self.interface.inputs):
            var = self.interface.inputs[k]
            if k not in kwargs:
                raise _user_exceptions.FlyteAssertion(
                    "Input was not specified for: {} of type {}".format(k, var.type)
                )
            bindings.append(flytekit_engine.binding_from_python_std(ctx, k, var.type, kwargs[k]))
            used_inputs.add(k)

        extra_inputs = used_inputs ^ set(kwargs.keys())
        if len(extra_inputs) > 0:
            raise _user_exceptions.FlyteAssertion(
                "Too many inputs were specified for the interface.  Extra inputs were: {}".format(extra_inputs)
            )

        # Detect upstream nodes
        upstream_nodes = [input_val.sdk_node for input_val in kwargs.values() if isinstance(input_val, _NodeOutput)]

        # TODO: Make the metadata name the full name of the (function)?
        sdk_node = _nodes.SdkNode(
            # TODO
            id=f"node-{len(ctx.compilation_state.nodes)}",
            metadata=_workflow_model.NodeMetadata(self._task_function.__name__, self.metadata.timeout,
                                                  self.metadata.retries,
                                                  self.metadata.interruptible),
            bindings=sorted(bindings, key=lambda b: b.var),
            upstream_nodes=upstream_nodes,
            sdk_task=self
        )
        ctx.compilation_state.nodes.append(sdk_node)

        # Create a node output object for each output, they should all point to this node of course.
        # TODO: Again, we need to be sure that we end up iterating through the output names in the correct order
        #  investigate this and document here.
        node_outputs = []
        for output_name, output_var_model in self.interface.outputs.items():
            # TODO: If node id gets updated later, we have to make sure to update the NodeOutput model's ID, which
            #  is currently just a static str
            node_outputs.append(_NodeOutput(sdk_node=sdk_node, sdk_type=None, var=output_name,
                                            literal_type=output_var_model.type))
        # Don't print this, it'll crash cuz sdk_node._upstream_node_ids might be None, but idl code will break

        if len(self.interface.outputs) > 1:
            return tuple(node_outputs)
        elif len(self.interface.outputs) == 1:
            return node_outputs[0]
        else:
            return None

    def dispatch_execute(self, ctx: FlyteContext,
                          input_literal_map: _literal_models.LiteralMap) -> _literal_models.LiteralMap:
        """
        This method translates Flytes native Type system based inputs and dispatches the actual call to the executor
        TODO We need to figure out for custom types what should be passed into runtime. I feel this should only be inputs,
            though in case of local execution we should pass the information of the task too.
        """
        # Translate the input literals to Python native
        native_inputs = flytekit_engine.idl_literal_map_to_python_value(ctx, input_literal_map)
        # TODO maybe we should replace the call of task_function to the actual executor. Thus instead of holding on to
        #     the function we just hold onto the executor
        native_outputs = self._task_function(**native_inputs)
        expected_output_names = list(self.interface.outputs.keys())
        if len(expected_output_names) == 1:
            native_outputs_as_map = {expected_output_names[0]: native_outputs}
        else:
            # Question: How do you know you're going to enumerate them in the correct order? Even if autonamed, will
            # output2 come before output100 if there's a hundred outputs? We don't! We'll have to circle back to
            # the Python task instance and inspect annotations again. Or we change the Python model representation
            # of the interface to be an ordered dict and we fill it in correctly to begin with.
            native_outputs_as_map = {expected_output_names[i]: native_outputs[i] for i, _ in
                                     enumerate(native_outputs)}

        # We manually construct a LiteralMap here because task inputs and outputs actually violate the assumption
        # built into the IDL that all the values of a literal map are of the same type.
        outputs_literal_map = _literal_models.LiteralMap(literals={
            k: flytekit_engine.python_value_to_idl_literal(ctx, v, self.interface.outputs[k].type) for k, v in
            native_outputs_as_map.items()
        })
        print("Outputs!")
        print(outputs_literal_map)
        return outputs_literal_map

    def __call__(self, *args, **kwargs):
        # When a Task is () aka __called__, there are three things we may do:
        #  a. Task Execution Mode - just run the Python function as Python normally would. Flyte steps completely
        #     out of the way.
        #  b. Compilation Mode - this happens when the function is called as part of a workflow (potentially
        #     dynamic task?). Instead of running the user function, produce promise objects and create a node.
        #  c. Workflow Execution Mode - when a workflow is being run locally. Even though workflows are functions
        #     and everything should be able to be passed through naturally, we'll want to wrap output values of the
        #     function into objects, so that potential .with_cpu or other ancillary functions can be attached to do
        #     nothing. Subsequent tasks will have to know how to unwrap these. If by chance a non-Flyte task uses a
        #     task output as an input, things probably will fail pretty obviously.
        if len(args) > 0:
            raise _user_exceptions.FlyteAssertion(
                f"When adding a task as a node in a workflow, all inputs must be specified with kwargs only.  We "
                f"detected {len(args)} positional args {args}"
            )

        ctx = FlyteContext.current_context()
        if ctx.compilation_state is not None and ctx.compilation_state.mode == 1:
            return self._create_and_link_node(ctx, *args, **kwargs)
        elif ctx.execution_state is not None and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION:
            # Unwrap the kwargs values. After this, we essentially have a LiteralMap
            for k, v in kwargs.items():
                if isinstance(v, _NodeOutput):
                    kwargs[k] = v.flyte_literal_value

            input_literal_map = _literal_models.LiteralMap(literals=kwargs)

            outputs_literal_map = self.dispatch_execute(ctx, input_literal_map)
            outputs_literals = outputs_literal_map.literals

            # TODO maybe this is the part that should be done for local execution, we pass the outputs to some special
            #    location, otherwise we dont really need to right? The higher level execute could just handle literalMap
            # After running, we again have to wrap the outputs, if any, back into NodeOutput objects
            output_names = list(self.interface.outputs.keys())
            node_results = []
            if len(output_names) != len(outputs_literals):
                # Length check, clean up exception
                raise Exception(f"Length difference {len(output_names)} {len(outputs_literals)}")

            if len(self.interface.outputs) > 1:
                for idx, var_name in enumerate(output_names):
                    node_results.append(_NodeOutput(sdk_node=A(), sdk_type=None,
                                                    var=output_names[idx],
                                                    flyte_literal_value=outputs_literals[var_name]))
                return tuple(node_results)
            elif len(self.interface.outputs) == 1:
                return _NodeOutput(sdk_node=A(), sdk_type=None, var=output_names[0],
                                   flyte_literal_value=outputs_literals[output_names[0]])
            else:
                return None

        else:
            # TODO: Remove warning
            logger.warning("task run without context - executing raw function")
            return self._task_function(*args, **kwargs)

    @property
    def interface(self) -> _common_interface.TypedInterface:
        return self._interface

    @property
    def metadata(self) -> _task_model.TaskMetadata:
        return self._metadata


def task(
        _task_function: Callable = None,
        task_type: str = "",
        cache: bool = False,
        cache_version: str = "",
        retries: int = 0,
        interruptible: bool = False,
        deprecated: str = "",
        timeout: Union[_datetime.timedelta, int] = None,
        environment: Dict[str, str] = None,
        *args, **kwargs):
    def wrapper(fn):
        _timeout = timeout
        if _timeout and not isinstance(_timeout, _datetime.timedelta):
            if isinstance(_timeout, int):
                _timeout = _datetime.timedelta(seconds=_timeout)
            else:
                raise ValueError("timeout should be duration represented as either a datetime.timedelta or int seconds")

        metadata = _task_model.TaskMetadata(
            discoverable=cache,
            runtime=_task_model.RuntimeMetadata(
                _task_model.RuntimeMetadata.RuntimeType.FLYTE_SDK,
                '1.2.3',
                'python'
            ),
            timeout=timeout,
            retries=_literal_models.RetryStrategy(retries),
            interruptible=interruptible,
            discovery_version=cache_version,
            deprecated_error_message=deprecated,
        )

        interface = get_interface_from_task_info(fn.__annotations__)

        task_instance = TaskTypePlugins[task_type](fn, interface, metadata, *args, **kwargs)
        # TODO: One of the things I want to make sure to do is better naming support. At this point, we should already
        #       be able to determine the name of the task right? Can anyone think of situations where we can't?
        #       Where does the current instance tracker come into play?
        task_instance.id = _identifier_model.Identifier(_identifier_model.ResourceType.TASK, "proj", "dom", "blah", "1")

        return task_instance

    if _task_function:
        return wrapper(_task_function)
    else:
        return wrapper


def get_interface_from_task_info(task_annotations: Dict[str, type]) -> _common_interface.TypedInterface:
    """
    From the annotations on a task function that the user should have provided, and the output names they want to use
    for each output parameter, construct the TypedInterface object

    For now the fancy object, maybe in the future a dumb object.

    :param task_annotations:
    :param output_names:
    """
    outputs_map = get_output_variable_map(task_annotations)

    inputs = OrderedDict()
    for k, v in task_annotations.items():
        if k != 'return':
            inputs[k] = v

    inputs_map = get_variable_map(inputs)
    interface_model = _interface_models.TypedInterface(inputs_map, outputs_map)

    # Maybe in the future we can just use the model
    return _common_interface.TypedInterface.promote_from_model(interface_model)


def get_variable_map(variable_map: Dict[str, type]) -> Dict[str, _interface_models.Variable]:
    """
    Given a map of str (names of inputs for instance) to their Python native types, return a map of the name to a
    Flyte Variable object with that type.
    """
    res = OrderedDict()
    e = type_engine.BaseEngine()
    for k, v in variable_map.items():
        res[k] = _interface_models.Variable(type=e.native_type_to_literal_type(v), description=k)

    return res


class Resources(object):
    def __init__(self, cpu, mem, gpu, storage):
        self._cpu = cpu
        self._mem = mem
        self._gpu = gpu
        self._storage = storage


TaskTypePlugins: DefaultDict[str, Type[Task]] = collections.defaultdict(
    lambda: Task,
    {
        "python_task": Task,
        "task": Task,
    }
)


def output_name_generator(length: int) -> Generator[str, None, None]:
    for x in range(0, length):
        yield f"out_{x}"


def get_output_variable_map(task_annotations: Dict[str, type]) -> Dict[str, _interface_models.Variable]:
    """
    Outputs can have various signatures, and we need to handle all of them:

        # Option 1
        nt1 = typing.NamedTuple("NT1", x_str=str, y_int=int)
        def t(a: int, b: str) -> nt1: ...

        # Option 2
        def t(a: int, b: str) -> typing.NamedTuple("NT1", x_str=str, y_int=int): ...

        # Option 3
        def t(a: int, b: str) -> typing.Tuple[int, str]: ...

        # Option 4
        def t(a: int, b: str) -> (int, str): ...

        # Option 5
        def t(a: int, b: str) -> str: ...

    TODO: We'll need to check the actual return types for in all cases as well, to make sure Flyte IDL actually
          supports it. For instance, typing.Tuple[Optional[int]] is not something we can represent currently.

    TODO: Generator[A,B,C] types are also valid, indicating dynamic tasks. Will need to implement.

    Note that Options 1 and 2 are identical, just syntactic sugar. In the NamedTuple case, we'll use the names in the
    definition. In all other cases, we'll automatically generate output names, indexed starting at 0.

    :param task_annotations: the __annotations__ attribute of a type hinted function.
    """
    if "return" in task_annotations:
        incoming_rt = task_annotations['return']

        # Handle options 1 and 2 first. The only way to check if the return type is a typing.NamedTuple, is to check
        # for this field. Using isinstance or issubclass doesn't work.
        if hasattr(incoming_rt, '_field_types'):
            logger.debug(f'Task returns named tuple {incoming_rt}')
            return_map = incoming_rt._field_types

        # Handle option 3
        elif hasattr(incoming_rt, '__origin__') and incoming_rt.__origin__ is tuple:
            logger.debug(f'Task returns unnamed typing.Tuple {incoming_rt}')
            return_types = incoming_rt.__args__
            return_names = [x for x in output_name_generator(len(incoming_rt.__args__))]
            return_map = OrderedDict(zip(return_names, return_types))

        # Handle option 4
        elif type(incoming_rt) is tuple:
            logger.debug(f'Task returns unnamed native tuple {incoming_rt}')
            return_names = [x for x in output_name_generator(len(incoming_rt))]
            return_map = OrderedDict(zip(return_names, incoming_rt))

        # Assume option 5
        else:
            logger.debug(f'Task returns a single output of type {incoming_rt}')
            return_map = {"out_0": incoming_rt}

        return get_variable_map(return_map)
    else:
        logger.debug(f'No return type found in annotations, returning empty map')
        # In the case where a task doesn't have a return type specified, assume that there are no outputs
        return {}