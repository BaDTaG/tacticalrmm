import asyncio
import datetime as dt
from loguru import logger
from tacticalrmm.celery import app
from django.conf import settings
import pytz
from django.utils import timezone as djangotime

from .models import AutomatedTask
from logs.models import PendingAction

logger.configure(**settings.LOG_CONFIG)


@app.task
def create_win_task_schedule(pk, pending_action=False):
    task = AutomatedTask.objects.get(pk=pk)

    if task.task_type == "scheduled":
        nats_data = {
            "func": "schedtask",
            "schedtaskpayload": {
                "type": "rmm",
                "trigger": "weekly",
                "weekdays": task.run_time_bit_weekdays,
                "pk": task.pk,
                "name": task.win_task_name,
                "hour": dt.datetime.strptime(task.run_time_minute, "%H:%M").hour,
                "min": dt.datetime.strptime(task.run_time_minute, "%H:%M").minute,
            },
        }

    elif task.task_type == "runonce":
        # check if scheduled time is in the past
        agent_tz = pytz.timezone(task.agent.timezone)
        task_time_utc = task.run_time_date.replace(tzinfo=agent_tz).astimezone(pytz.utc)
        now = djangotime.now()
        if task_time_utc < now:
            task.run_time_date = now.astimezone(agent_tz).replace(
                tzinfo=pytz.utc
            ) + djangotime.timedelta(minutes=5)
            task.save()

        nats_data = {
            "func": "schedtask",
            "schedtaskpayload": {
                "type": "rmm",
                "trigger": "once",
                "pk": task.pk,
                "name": task.win_task_name,
                "year": int(dt.datetime.strftime(task.run_time_date, "%Y")),
                "month": dt.datetime.strftime(task.run_time_date, "%B"),
                "day": int(dt.datetime.strftime(task.run_time_date, "%d")),
                "hour": int(dt.datetime.strftime(task.run_time_date, "%H")),
                "min": int(dt.datetime.strftime(task.run_time_date, "%M")),
            },
        }

    elif task.task_type == "checkfailure" or task.task_type == "manual":
        nats_data = {
            "func": "schedtask",
            "schedtaskpayload": {
                "type": "rmm",
                "trigger": "manual",
                "pk": task.pk,
                "name": task.win_task_name,
            },
        }
    else:
        return "error"

    r = asyncio.run(task.agent.nats_cmd(nats_data, timeout=10))
    if r != "ok":
        # don't create pending action if this task was initiated by a pending action
        if not pending_action:
            PendingAction(
                agent=task.agent,
                action_type="taskaction",
                details={"action": "taskcreate", "task_id": task.id},
            ).save()
            task.sync_status = "notsynced"
            task.save(update_fields=["sync_status"])

        logger.error(
            f"Unable to create scheduled task {task.win_task_name} on {task.agent.hostname}. It will be created when the agent checks in."
        )
        return

    # clear pending action since it was successful
    if pending_action:
        pendingaction = PendingAction.objects.get(pk=pending_action)
        pendingaction.status = "completed"
        pendingaction.save(update_fields=["status"])

    task.sync_status = "synced"
    task.save(update_fields=["sync_status"])

    logger.info(f"{task.agent.hostname} task {task.name} was successfully created")

    return "ok"


@app.task
def enable_or_disable_win_task(pk, action, pending_action=False):
    task = AutomatedTask.objects.get(pk=pk)

    nats_data = {
        "func": "enableschedtask",
        "schedtaskpayload": {
            "name": task.win_task_name,
            "enabled": action,
        },
    }
    r = asyncio.run(task.agent.nats_cmd(nats_data))

    if r != "ok":
        # don't create pending action if this task was initiated by a pending action
        if not pending_action:
            PendingAction(
                agent=task.agent,
                action_type="taskaction",
                details={
                    "action": "tasktoggle",
                    "value": action,
                    "task_id": task.id,
                },
            ).save()
            task.sync_status = "notsynced"
            task.save(update_fields=["sync_status"])

        return

    # clear pending action since it was successful
    if pending_action:
        pendingaction = PendingAction.objects.get(pk=pending_action)
        pendingaction.status = "completed"
        pendingaction.save(update_fields=["status"])

    task.sync_status = "synced"
    task.save(update_fields=["sync_status"])
    return "ok"


@app.task
def delete_win_task_schedule(pk, pending_action=False):
    task = AutomatedTask.objects.get(pk=pk)

    nats_data = {
        "func": "delschedtask",
        "schedtaskpayload": {"name": task.win_task_name},
    }
    r = asyncio.run(task.agent.nats_cmd(nats_data, timeout=10))

    if r != "ok":
        # don't create pending action if this task was initiated by a pending action
        if not pending_action:
            PendingAction(
                agent=task.agent,
                action_type="taskaction",
                details={"action": "taskdelete", "task_id": task.id},
            ).save()
            task.sync_status = "pendingdeletion"
            task.save(update_fields=["sync_status"])

        return

    # complete pending action since it was successful
    if pending_action:
        pendingaction = PendingAction.objects.get(pk=pending_action)
        pendingaction.status = "completed"
        pendingaction.save(update_fields=["status"])

    task.delete()
    return "ok"


@app.task
def run_win_task(pk):
    task = AutomatedTask.objects.get(pk=pk)
    asyncio.run(task.agent.nats_cmd({"func": "runtask", "taskpk": task.pk}, wait=False))
    return "ok"


@app.task
def remove_orphaned_win_tasks(agentpk):
    from agents.models import Agent

    agent = Agent.objects.get(pk=agentpk)

    logger.info(f"Orphaned task cleanup initiated on {agent.hostname}.")

    r = asyncio.run(agent.nats_cmd({"func": "listschedtasks"}, timeout=10))

    if not isinstance(r, list) and not r:  # empty list
        logger.error(f"Unable to clean up scheduled tasks on {agent.hostname}: {r}")
        return "notlist"

    agent_task_names = list(agent.autotasks.values_list("win_task_name", flat=True))

    exclude_tasks = (
        "TacticalRMM_fixmesh",
        "TacticalRMM_SchedReboot",
    )

    for task in r:
        if task.startswith(exclude_tasks):
            # skip system tasks or any pending reboots
            continue

        if task.startswith("TacticalRMM_") and task not in agent_task_names:
            # delete task since it doesn't exist in UI
            nats_data = {
                "func": "delschedtask",
                "schedtaskpayload": {"name": task},
            }
            ret = asyncio.run(agent.nats_cmd(nats_data, timeout=10))
            if ret != "ok":
                logger.error(
                    f"Unable to clean up orphaned task {task} on {agent.hostname}: {ret}"
                )
            else:
                logger.info(f"Removed orphaned task {task} from {agent.hostname}")

    logger.info(f"Orphaned task cleanup finished on {agent.hostname}")
