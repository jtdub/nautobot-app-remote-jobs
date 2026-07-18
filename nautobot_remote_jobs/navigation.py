"""Navigation menu: "Remote Jobs" (SPEC 15)."""

from nautobot.apps.ui import NavMenuAddButton, NavMenuGroup, NavMenuItem, NavMenuTab

menu_items = (
    NavMenuTab(
        name="Remote Jobs",
        weight=750,
        groups=(
            NavMenuGroup(
                name="Jobs",
                weight=100,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:jobdefinition_list",
                        name="Job Definitions",
                        permissions=["nautobot_remote_jobs.view_jobdefinition"],
                        buttons=(
                            NavMenuAddButton(
                                link="plugins:nautobot_remote_jobs:jobdefinition_add",
                                permissions=["nautobot_remote_jobs.add_jobdefinition"],
                            ),
                        ),
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:remotejobrun_list",
                        name="Runs",
                        permissions=["nautobot_remote_jobs.view_remotejobrun"],
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:remotejobschedule_list",
                        name="Schedules",
                        permissions=["nautobot_remote_jobs.view_remotejobschedule"],
                        buttons=(
                            NavMenuAddButton(
                                link="plugins:nautobot_remote_jobs:remotejobschedule_add",
                                permissions=["nautobot_remote_jobs.add_remotejobschedule"],
                            ),
                        ),
                    ),
                ),
            ),
            NavMenuGroup(
                name="Topology",
                weight=200,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:executionzone_list",
                        name="Execution Zones",
                        permissions=["nautobot_remote_jobs.view_executionzone"],
                        buttons=(
                            NavMenuAddButton(
                                link="plugins:nautobot_remote_jobs:executionzone_add",
                                permissions=["nautobot_remote_jobs.add_executionzone"],
                            ),
                        ),
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:zonemembershiprule_list",
                        name="Zone Membership Rules",
                        permissions=["nautobot_remote_jobs.view_zonemembershiprule"],
                        buttons=(
                            NavMenuAddButton(
                                link="plugins:nautobot_remote_jobs:zonemembershiprule_add",
                                permissions=["nautobot_remote_jobs.add_zonemembershiprule"],
                            ),
                        ),
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:zone_coverage",
                        name="Zone Coverage",
                        permissions=["nautobot_remote_jobs.view_executionzone"],
                    ),
                ),
            ),
            NavMenuGroup(
                name="Workers",
                weight=300,
                items=(
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:worker_list",
                        name="Workers",
                        permissions=["nautobot_remote_jobs.view_worker"],
                    ),
                    NavMenuItem(
                        link="plugins:nautobot_remote_jobs:workerenrollmenttoken_list",
                        name="Enrollment Tokens",
                        permissions=["nautobot_remote_jobs.view_workerenrollmenttoken"],
                        buttons=(
                            NavMenuAddButton(
                                link="plugins:nautobot_remote_jobs:workerenrollmenttoken_add",
                                permissions=["nautobot_remote_jobs.add_workerenrollmenttoken"],
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
)
