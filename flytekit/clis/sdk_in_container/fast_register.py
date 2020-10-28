import logging as _logging
from pathlib import Path

import click
import os

from flytekit.clis.sdk_in_container.constants import CTX_DOMAIN, CTX_PACKAGES, CTX_PROJECT, CTX_TEST, CTX_VERSION, CTX_CURRENT_DIR
from flytekit.common import utils as _utils
from flytekit.common.core import identifier as _identifier
from flytekit.common.tasks import task as _task
from flytekit.configuration.internal import IMAGE as _IMAGE
from flytekit.configuration.internal import look_up_version_from_image_tag as _look_up_version_from_image_tag
from flytekit.tools.module_loader import iterate_registerable_entities_in_order
from flytekit.tools.fast_registration import upload_package, compute_digest
from flytekit.configuration import aws as _aws_config


def fast_register_all(project, domain, pkgs, test, version, source_dir):
    if test:
        click.echo("Test switch enabled, not doing anything...")

    digest = compute_digest(source_dir)
    upload_package(source_dir, digest, _aws_config.FAST_REGISTRATION_DIR.get())

    click.echo(
        "Running task, workflow, and launch plan fast registration for {}, {}, {} with version {} and code dir {}".
            format(project, domain, pkgs, version, source_dir)
    )

    # m = module (i.e. python file)
    # k = value of dir(m), type str
    # o = object (e.g. SdkWorkflow)
    for m, k, o in iterate_registerable_entities_in_order(pkgs):
        name = _utils.fqdn(m.__name__, k, entity_type=o.resource_type)
        o._id = _identifier.Identifier(o.resource_type, project, domain, name, version)

        if test:
            click.echo("Would fast register {:20} {}".format("{}:".format(o.entity_type_text), o.id.name))
        else:
            click.echo("Fast registering {:20} {}".format("{}:".format(o.entity_type_text), o.id.name))
            o.fast_register(project, domain, o.id.name, already_uploaded_digest=digest)


def fast_register_tasks_only(project: str, domain: str, pkgs, test: bool, version: str, source_dir: os.PathLike):
    if test:
        click.echo("Test switch enabled, not doing anything...")

    digest = compute_digest(source_dir)
    upload_package(source_dir, digest, _aws_config.FAST_REGISTRATION_DIR.get())

    click.echo("Running task only fast registration for {}, {}, {} with version {} and code dir {}".format(
        project, domain, pkgs, version, source_dir))

    # Discover all tasks by loading the module
    for m, k, t in iterate_registerable_entities_in_order(pkgs, include_entities={_task.SdkTask}):
        name = _utils.fqdn(m.__name__, k, entity_type=t.resource_type)

        if test:
            click.echo("Would fast register task {:20} {}".format("{}:".format(t.entity_type_text), name))
        else:
            click.echo("Fast registering task {:20} {}".format("{}:".format(t.entity_type_text), name))
            t.fast_register(project, domain, name, already_uploaded_digest=digest)


@click.group("fast-register")
@click.option("--test", is_flag=True, help="Dry run, do not actually register with Admin")
@click.pass_context
def fast_register(ctx, test=None):
    """
    Run registration steps for the workflows in this container.

    Run with the --test switch for a dry run to see what will be registered.  A default launch plan will also be
    created, if a role can be found in the environment variables.
    """

    ctx.obj[CTX_TEST] = test
    ctx.obj[CTX_CURRENT_DIR] = os.getcwd()


@click.command("tasks")
@click.option(
    "-v",
    "--version",
    type=str,
    help="Version to register tasks with. This is normally parsed from the" "image, but you can override here.",
)
@click.pass_context
def tasks(ctx, version=None):
    """
    Only fast register tasks.
    """
    project = ctx.obj[CTX_PROJECT]
    domain = ctx.obj[CTX_DOMAIN]
    test = ctx.obj[CTX_TEST]
    pkgs = ctx.obj[CTX_PACKAGES]
    source_dir = Path(ctx.obj[CTX_CURRENT_DIR])

    version = version or ctx.obj[CTX_VERSION] or _look_up_version_from_image_tag(_IMAGE.get())
    fast_register_tasks_only(project, domain, pkgs, test, version, source_dir)


@click.command("workflows")
@click.option(
    "-v",
    "--version",
    type=str,
    help="Version to register tasks with. This is normally parsed from the" "image, but you can override here.",
)
@click.pass_context
def workflows(ctx, version=None):
    """
    Fast register both tasks and workflows.  Also create and register a default launch plan for all workflows.
    """
    project = ctx.obj[CTX_PROJECT]
    domain = ctx.obj[CTX_DOMAIN]
    test = ctx.obj[CTX_TEST]
    pkgs = ctx.obj[CTX_PACKAGES]
    source_dir = Path(ctx.obj[CTX_CURRENT_DIR])

    version = version or ctx.obj[CTX_VERSION] or _look_up_version_from_image_tag(_IMAGE.get())
    fast_register_all(project, domain, pkgs, test, version, source_dir)


fast_register.add_command(tasks)
fast_register.add_command(workflows)