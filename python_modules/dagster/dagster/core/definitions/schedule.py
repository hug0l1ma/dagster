from contextlib import ExitStack
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Union, cast

import pendulum
from croniter import croniter
from dagster import check
from dagster.core.errors import (
    DagsterInvalidDefinitionError,
    ScheduleExecutionError,
    user_code_error_boundary,
)
from dagster.core.instance import DagsterInstance
from dagster.core.instance.ref import InstanceRef
from dagster.core.storage.pipeline_run import PipelineRun
from dagster.core.storage.tags import check_tags
from dagster.utils import ensure_gen, merge_dicts
from dagster.utils.backcompat import experimental_fn_warning

from .mode import DEFAULT_MODE_NAME
from .run_request import JobType, RunRequest, SkipReason
from .utils import check_valid_name


class ScheduleExecutionContext:
    """Schedule-specific execution context.

    An instance of this class is made available as the first argument to various ScheduleDefinition
    functions. It is passed as the first argument to ``run_config_fn``, ``tags_fn``,
    and ``should_execute``.

    Attributes:
        instance_ref (InstanceRef): The serialized instance configured to run the schedule
        scheduled_execution_time (datetime):
            The time in which the execution was scheduled to happen. May differ slightly
            from both the actual execution time and the time at which the run config is computed.
            Not available in all schedulers - currently only set in deployments using
            DagsterDaemonScheduler.
    """

    __slots__ = ["_instance_ref", "_scheduled_execution_time", "_exit_stack", "_instance"]

    def __init__(self, instance_ref: InstanceRef, scheduled_execution_time: Optional[datetime]):
        self._exit_stack = ExitStack()
        self._instance = None

        self._instance_ref = check.inst_param(instance_ref, "instance_ref", InstanceRef)
        self._scheduled_execution_time = check.opt_inst_param(
            scheduled_execution_time, "scheduled_execution_time", datetime
        )

    def __enter__(self):
        return self

    def __exit__(self, _exception_type, _exception_value, _traceback):
        self._exit_stack.close()

    @property
    def instance(self) -> "DagsterInstance":
        if not self._instance:
            self._instance = self._exit_stack.enter_context(
                DagsterInstance.from_ref(self._instance_ref)
            )
        return cast(DagsterInstance, self._instance)

    @property
    def scheduled_execution_time(self) -> Optional[datetime]:
        return self._scheduled_execution_time


def build_schedule_context(
    instance: DagsterInstance, scheduled_execution_time: Optional[datetime] = None
) -> ScheduleExecutionContext:
    """Builds schedule execution context using the provided parameters.

    The instance provided to ``build_schedule_context`` must be persistent;
    DagsterInstance.ephemeral() will result in an error.

    Args:
        instance (DagsterInstance): The dagster instance configured to run the schedule.
        scheduled_execution_time (datetime): The time in which the execution was scheduled to
            happen. May differ slightly from both the actual execution time and the time at which
            the run config is computed.

    Examples:

        .. code-block:: python

            context = build_schedule_context(instance)
            daily_schedule.get_execution_data(context)

    """

    experimental_fn_warning("build_schedule_context")

    check.inst_param(instance, "instance", DagsterInstance)
    return ScheduleExecutionContext(
        instance_ref=instance.get_ref(),
        scheduled_execution_time=check.opt_inst_param(
            scheduled_execution_time, "scheduled_execution_time", datetime
        ),
    )


class ScheduleDefinition:
    """Define a schedule that targets a pipeline

    Args:
        name (str): The name of the schedule to create.
        cron_schedule (str): A valid cron string specifying when the schedule will run, e.g.,
            '45 23 * * 6' for a schedule that runs at 11:45 PM every Saturday.
        pipeline_name (str): The name of the pipeline to execute when the schedule runs.
        execution_fn (Callable[ScheduleExecutionContext]): The core evaluation function for the
            schedule, which is run at an interval to determine whether a run should be launched or
            not. Takes a :py:class:`~dagster.ScheduleExecutionContext`.

            This function must return a generator, which must yield either a single SkipReason
            or one or more RunRequest objects.
        run_config (Optional[Dict]): The environment config that parameterizes this execution,
            as a dict.
        run_config_fn (Optional[Callable[[ScheduleExecutionContext], [Dict]]]): A function that
            takes a ScheduleExecutionContext object and returns the environment configuration that
            parameterizes this execution, as a dict. You may set only one of ``run_config``,
            ``run_config_fn``, and ``execution_fn``.
        tags (Optional[Dict[str, str]]): A dictionary of tags (string key-value pairs) to attach
            to the scheduled runs.
        tags_fn (Optional[Callable[[ScheduleExecutionContext], Optional[Dict[str, str]]]]): A
            function that generates tags to attach to the schedules runs. Takes a
            :py:class:`~dagster.ScheduleExecutionContext` and returns a dictionary of tags (string
            key-value pairs). You may set only one of ``tags``, ``tags_fn``, and ``execution_fn``.
        solid_selection (Optional[List[str]]): A list of solid subselection (including single
            solid names) to execute when the schedule runs. e.g. ``['*some_solid+', 'other_solid']``
        mode (Optional[str]): The mode to apply when executing this schedule. (default: 'default')
        should_execute (Optional[Callable[[ScheduleExecutionContext], bool]]): A function that runs
            at schedule execution time to determine whether a schedule should execute or skip. Takes
            a :py:class:`~dagster.ScheduleExecutionContext` and returns a boolean (``True`` if the
            schedule should execute). Defaults to a function that always returns ``True``.
        environment_vars (Optional[dict[str, str]]): The environment variables to set for the
            schedule
        execution_timezone (Optional[str]): Timezone in which the schedule should run. Only works
            with DagsterDaemonScheduler, and must be set when using that scheduler.
        description (Optional[str]): A human-readable description of the schedule.
    """

    __slots__ = [
        "_name",
        "_pipeline_name",
        "_tags_fn",
        "_run_config_fn",
        "_mode",
        "_solid_selection",
        "_description",
        "_cron_schedule",
        "_environment_vars",
        "_execution_fn",
        "_execution_timezone",
    ]

    def __init__(
        self,
        name: str,
        cron_schedule: str,
        pipeline_name: str,
        run_config: Optional[Any] = None,
        run_config_fn: Optional[Callable[..., Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        tags_fn: Optional[Callable[..., Optional[Dict[str, str]]]] = None,
        solid_selection: Optional[List[Any]] = None,
        mode: Optional[str] = "default",
        should_execute: Optional[Callable[..., bool]] = None,
        environment_vars: Optional[Dict[str, str]] = None,
        execution_timezone: Optional[str] = None,
        execution_fn: Optional[Callable[[ScheduleExecutionContext], Any]] = None,
        description: Optional[str] = None,
    ):

        if not croniter.is_valid(cron_schedule):
            raise DagsterInvalidDefinitionError(
                f"Found invalid cron schedule '{cron_schedule}' for schedule '{name}''."
            )

        self._name = check_valid_name(name)
        self._pipeline_name = check.str_param(pipeline_name, "pipeline_name")
        self._mode = cast(str, check.opt_str_param(mode, "mode", DEFAULT_MODE_NAME))
        self._solid_selection = check.opt_nullable_list_param(
            solid_selection, "solid_selection", of_type=str
        )
        self._description = check.opt_str_param(description, "description")

        self._cron_schedule = check.str_param(cron_schedule, "cron_schedule")
        self._environment_vars = check.opt_dict_param(
            environment_vars, "environment_vars", key_type=str, value_type=str
        )
        self._execution_timezone = check.opt_str_param(execution_timezone, "execution_timezone")

        if execution_fn and (run_config_fn or tags_fn or should_execute or tags or run_config):
            raise DagsterInvalidDefinitionError(
                "Attempted to provide both execution_fn and individual run_config/tags arguments "
                "to ScheduleDefinition. Must provide only one of the two."
            )
        elif execution_fn:
            self._execution_fn = check.opt_callable_param(execution_fn, "execution_fn")
        else:
            if run_config_fn and run_config:
                raise DagsterInvalidDefinitionError(
                    "Attempted to provide both run_config_fn and run_config as arguments"
                    " to ScheduleDefinition. Must provide only one of the two."
                )
            run_config_fn = check.opt_callable_param(
                run_config_fn,
                "run_config_fn",
                default=lambda _context: check.opt_dict_param(run_config, "run_config"),
            )

            if tags_fn and tags:
                raise DagsterInvalidDefinitionError(
                    "Attempted to provide both tags_fn and tags as arguments"
                    " to ScheduleDefinition. Must provide only one of the two."
                )
            elif tags:
                check_tags(tags, "tags")
                tags_fn = lambda _context: tags
            else:
                tags_fn = check.opt_callable_param(tags_fn, "tags_fn", default=lambda _context: {})

            should_execute = check.opt_callable_param(
                should_execute, "should_execute", default=lambda _context: True
            )

            def _execution_fn(context):
                with user_code_error_boundary(
                    ScheduleExecutionError,
                    lambda: f"Error occurred during the execution of should_execute for schedule {name}",
                ):
                    if not should_execute(context):
                        yield SkipReason(
                            "should_execute function for {schedule_name} returned false.".format(
                                schedule_name=name
                            )
                        )
                        return

                with user_code_error_boundary(
                    ScheduleExecutionError,
                    lambda: f"Error occurred during the execution of run_config_fn for schedule {name}",
                ):
                    evaluated_run_config = run_config_fn(context)

                with user_code_error_boundary(
                    ScheduleExecutionError,
                    lambda: f"Error occurred during the execution of tags_fn for schedule {name}",
                ):
                    evaluated_tags = tags_fn(context)

                yield RunRequest(
                    run_key=None,
                    run_config=evaluated_run_config,
                    tags=evaluated_tags,
                )

            self._execution_fn = _execution_fn

        if self._execution_timezone:
            try:
                # Verify that the timezone can be loaded
                pendulum.timezone(self._execution_timezone)
            except Exception:
                raise DagsterInvalidDefinitionError(
                    "Invalid execution timezone {timezone} for {schedule_name}".format(
                        schedule_name=name, timezone=self._execution_timezone
                    )
                )

    @property
    def name(self) -> str:
        return self._name

    @property
    def pipeline_name(self) -> str:
        return self._pipeline_name

    @property
    def job_type(self) -> JobType:
        return JobType.SCHEDULE

    @property
    def solid_selection(self) -> Optional[List[Any]]:
        return self._solid_selection

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def cron_schedule(self) -> str:
        return self._cron_schedule

    @property
    def environment_vars(self) -> Dict[str, str]:
        return self._environment_vars

    @property
    def execution_timezone(self) -> Optional[str]:
        return self._execution_timezone

    def get_execution_data(
        self, context: "ScheduleExecutionContext"
    ) -> List[Union[RunRequest, SkipReason]]:
        check.inst_param(context, "context", ScheduleExecutionContext)
        execution_fn = cast(Callable[[ScheduleExecutionContext], Any], self._execution_fn)
        result = list(ensure_gen(execution_fn(context)))

        if not result:
            return []

        if len(result) == 1:
            check.is_list(result, of_type=(RunRequest, SkipReason))
            data = result[0]

            if isinstance(data, SkipReason):
                return result
            check.inst(data, RunRequest)
            return [
                RunRequest(
                    run_key=data.run_key,
                    run_config=data.run_config,
                    tags=merge_dicts(data.tags, PipelineRun.tags_for_schedule(self)),
                )
            ]

        check.is_list(result, of_type=RunRequest)

        check.invariant(
            not any(not data.run_key for data in result),
            "Schedules that return multiple RunRequests must specify a run_key in each RunRequest",
        )

        # clone all the run requests with the required schedule tags
        return [
            RunRequest(
                run_key=data.run_key,
                run_config=data.run_config,
                tags=merge_dicts(data.tags, PipelineRun.tags_for_schedule(self)),
            )
            for data in result
        ]
