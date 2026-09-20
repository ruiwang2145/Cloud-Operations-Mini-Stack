"""``manage.py seed_tasks`` -- put data in the database.

Why a management command rather than a fixture
----------------------------------------------
A fixture is a frozen file that has to be regenerated whenever the model changes,
and it produces the same rows every time -- which makes it useless for the thing
this data is actually for: making latency and traffic graphs look like something.

A command can generate a realistic spread of priorities, completion states and
creation times, and it is idempotent enough to be run repeatedly during a demo.
"""
from __future__ import annotations

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tasks.models import Task

TITLES = [
    "Write the runbook for a failed deploy",
    "Add a latency panel to the overview dashboard",
    "Investigate the 502s on the payments endpoint",
    "Rotate the database credentials",
    "Reduce the alert noise from readiness probes",
    "Document the rollback procedure",
    "Review the retention setting on the log volume",
    "Add an index for the done/created_at filter",
    "Pin the base image to a digest",
    "Close the stale incident from last month",
    "Check the error budget burn for the week",
    "Move the smoke test into CI",
    "Upgrade the Prometheus version",
    "Delete the unused feature flag",
    "Confirm the backup restore actually works",
    "Add a timeout to the outbound HTTP client",
    "Split the monolithic settings module",
    "Trim the container image size",
    "Write the post-incident review",
    "Tune the histogram buckets",
]


class Command(BaseCommand):
    help = "Create sample tasks so the dashboards and the API have data to show."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--count", type=int, default=25, help="how many tasks to create (default: 25)"
        )
        parser.add_argument(
            "--done-ratio", type=float, default=0.4,
            help="fraction of tasks marked complete (default: 0.4)",
        )
        parser.add_argument(
            "--spread-days", type=int, default=30,
            help="spread created_at over the last N days (default: 30)",
        )
        parser.add_argument(
            "--seed", type=int, default=None,
            help="random seed, so a demo produces the same data every time",
        )
        parser.add_argument(
            "--clear", action="store_true", help="delete existing tasks first",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        rng = random.Random(options["seed"])
        count = max(0, options["count"])

        if options["clear"]:
            deleted, _ = Task.objects.all().delete()
            self.stdout.write(f"deleted {deleted} existing row(s)")

        priorities = [choice for choice, _ in Task.Priority.choices]
        now = timezone.now()
        created: list[Task] = []

        for index in range(count):
            created.append(
                Task(
                    title=f"{rng.choice(TITLES)} #{index + 1}",
                    notes=rng.choice(
                        ["", "", "Raised during the incident review.", "Blocks the release."]
                    ),
                    done=rng.random() < options["done_ratio"],
                    priority=rng.choice(priorities),
                )
            )

        Task.objects.bulk_create(created)

        # `auto_now_add` stamps everything with the same instant, so the spread is
        # applied afterwards with a bulk update. Without this, every graph that
        # plots tasks over time shows a single vertical spike on the day the demo
        # was run -- which looks like a bug and teaches a reviewer nothing.
        if options["spread_days"] > 0 and created:
            span = timedelta(days=options["spread_days"])
            # Re-query to get primary keys; bulk_create does not reliably set them
            # on every backend.
            saved = list(Task.objects.order_by("-id")[: len(created)])
            for task in saved:
                task.created_at = now - timedelta(seconds=rng.random() * span.total_seconds())
            Task.objects.bulk_update(saved, ["created_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"created {Task.objects.count()} task(s) "
                f"({Task.objects.filter(done=True).count()} complete)"
            )
        )
