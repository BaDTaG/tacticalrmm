from datetime import datetime, timedelta
from model_bakery import baker, seq
from tacticalrmm.test import TacticalTestCase
from .serializers import PendingActionSerializer
from unittest.mock import patch


class TestAuditViews(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

    def create_audit_records(self):

        # create clients for client filter
        site = baker.make("clients.Site")
        baker.make_recipe("agents.agent", site=site, hostname="AgentHostname1")
        # user jim agent logs
        baker.make_recipe(
            "logs.agent_logs",
            username="jim",
            agent="AgentHostname1",
            entry_time=seq(datetime.now(), timedelta(days=3)),
            _quantity=15,
        )
        baker.make_recipe(
            "logs.agent_logs",
            username="jim",
            agent="AgentHostname2",
            entry_time=seq(datetime.now(), timedelta(days=100)),
            _quantity=8,
        )

        # user james agent logs
        baker.make_recipe(
            "logs.agent_logs",
            username="james",
            agent="AgentHostname1",
            entry_time=seq(datetime.now(), timedelta(days=55)),
            _quantity=7,
        )
        baker.make_recipe(
            "logs.agent_logs",
            username="james",
            agent="AgentHostname2",
            entry_time=seq(datetime.now(), timedelta(days=20)),
            _quantity=10,
        )

        # generate agent logs with random usernames
        baker.make_recipe(
            "logs.agent_logs",
            agent=seq("AgentHostname"),
            entry_time=seq(datetime.now(), timedelta(days=29)),
            _quantity=5,
        )

        # generate random object data
        baker.make_recipe(
            "logs.object_logs",
            username="james",
            entry_time=seq(datetime.now(), timedelta(days=5)),
            _quantity=17,
        )

        # generate login data for james
        baker.make_recipe(
            "logs.login_logs",
            username="james",
            entry_time=seq(datetime.now(), timedelta(days=7)),
            _quantity=11,
        )

        # generate login data for jim
        baker.make_recipe(
            "logs.login_logs",
            username="jim",
            entry_time=seq(datetime.now(), timedelta(days=11)),
            _quantity=13,
        )

        return site

    def test_get_audit_logs(self):
        url = "/logs/auditlogs/"

        # create data
        site = self.create_audit_records()

        # test data and result counts
        data = [
            {"filter": {"timeFilter": 30}, "count": 86},
            {
                "filter": {"timeFilter": 45, "agentFilter": ["AgentHostname2"]},
                "count": 19,
            },
            {
                "filter": {"userFilter": ["jim"], "agentFilter": ["AgentHostname1"]},
                "count": 15,
            },
            {
                "filter": {
                    "timeFilter": 180,
                    "userFilter": ["james"],
                    "agentFilter": ["AgentHostname1"],
                },
                "count": 7,
            },
            {"filter": {}, "count": 86},
            {"filter": {"agentFilter": ["DoesntExist"]}, "count": 0},
            {
                "filter": {
                    "timeFilter": 35,
                    "userFilter": ["james", "jim"],
                    "agentFilter": ["AgentHostname1", "AgentHostname2"],
                },
                "count": 40,
            },
            {"filter": {"timeFilter": 35, "userFilter": ["james", "jim"]}, "count": 81},
            {"filter": {"objectFilter": ["user"]}, "count": 26},
            {"filter": {"actionFilter": ["login"]}, "count": 12},
            {"filter": {"clientFilter": [site.client.id]}, "count": 23},
        ]

        for req in data:
            resp = self.client.patch(url, req["filter"], format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.data), req["count"])

        self.check_not_authenticated("patch", url)

    def test_options_filter(self):
        url = "/logs/auditlogs/optionsfilter/"

        baker.make("agents.Agent", hostname=seq("AgentHostname"), _quantity=5)
        baker.make("agents.Agent", hostname=seq("Server"), _quantity=3)
        baker.make("accounts.User", username=seq("Username"), _quantity=7)
        baker.make("accounts.User", username=seq("soemthing"), _quantity=3)

        data = [
            {"req": {"type": "agent", "pattern": "AgeNt"}, "count": 5},
            {"req": {"type": "agent", "pattern": "AgentHostname1"}, "count": 1},
            {"req": {"type": "agent", "pattern": "hasjhd"}, "count": 0},
            {"req": {"type": "user", "pattern": "UsEr"}, "count": 7},
            {"req": {"type": "user", "pattern": "UserName1"}, "count": 1},
            {"req": {"type": "user", "pattern": "dfdsadf"}, "count": 0},
        ]

        for req in data:
            resp = self.client.post(url, req["req"], format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.data), req["count"])

        # test for invalid payload. needs to have either type: user or agent
        invalid_data = {"type": "object", "pattern": "SomeString"}

        resp = self.client.post(url, invalid_data, format="json")
        self.assertEqual(resp.status_code, 400)

        self.check_not_authenticated("post", url)

    def test_agent_pending_actions(self):
        agent = baker.make_recipe("agents.agent")
        pending_actions = baker.make(
            "logs.PendingAction",
            agent=agent,
            _quantity=6,
        )
        url = f"/logs/{agent.pk}/pendingactions/"

        resp = self.client.get(url, format="json")
        serializer = PendingActionSerializer(pending_actions, many=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 6)
        self.assertEqual(resp.data, serializer.data)

        self.check_not_authenticated("get", url)

    def test_all_pending_actions(self):
        url = "/logs/allpendingactions/"
        pending_actions = baker.make("logs.PendingAction", _quantity=6)

        resp = self.client.get(url, format="json")
        serializer = PendingActionSerializer(pending_actions, many=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 6)
        self.assertEqual(resp.data, serializer.data)

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_cancel_pending_action(self, nats_cmd):
        url = "/logs/cancelpendingaction/"
        # TODO fix this TypeError: Object of type coroutine is not JSON serializable
        """ agent = baker.make("agents.Agent", version="1.1.1")
        pending_action = baker.make(
            "logs.PendingAction",
            agent=agent,
            details={
                "time": "2021-01-13 18:20:00",
                "taskname": "TacticalRMM_SchedReboot_wYzCCDVXlc",
            },
        )

        data = {"pk": pending_action.id}
        resp = self.client.delete(url, data, format="json")
        self.assertEqual(resp.status_code, 200)
        nats_data = {
            "func": "delschedtask",
            "schedtaskpayload": {"name": "TacticalRMM_SchedReboot_wYzCCDVXlc"},
        }
        nats_cmd.assert_called_with(nats_data, timeout=10)

        # try request again and it should fail since pending action doesn't exist
        resp = self.client.delete(url, data, format="json")
        self.assertEqual(resp.status_code, 404) """

        self.check_not_authenticated("delete", url)
