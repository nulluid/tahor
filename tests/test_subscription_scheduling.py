from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run
import setup_tahor
import subscription_bulk
import subscription_suggestions
from test_system_services import services


class SubscriptionSchedulingTests(unittest.TestCase):
    def test_separate_bounded_passes_do_not_run_other_queues(self):
        with patch.object(subscription_suggestions, 'run_pending_jobs', return_value=0) as models, patch.object(subscription_bulk, 'run_pending') as actions:
            self.assertEqual(subscription_suggestions.main(), 0)
            models.assert_called_once_with(max_jobs=5)
            actions.assert_not_called()
        with patch.object(subscription_suggestions, 'run_pending_jobs') as models, patch.object(subscription_bulk, 'run_pending', return_value={'processed': 4}) as actions:
            self.assertEqual(subscription_bulk.main(), 0)
            actions.assert_called_once_with(limit=10)
            models.assert_not_called()
        with patch.object(subscription_suggestions, 'run_pending_jobs', return_value=1):
            self.assertEqual(subscription_suggestions.main(), 1)

    def test_dispatch_loads_private_environment_and_chooses_only_requested_queue(self):
        for command, script in [('subscriptions', 'subscription_suggestions.py'), ('subscription-actions', 'subscription_bulk.py')]:
            with patch.object(run.sys, 'argv', ['run.py', '--env', '/private/config.env', command]), patch.object(run, 'load_environment') as load, patch('tahor_db.init_db'), patch.object(run.os, 'execv') as execute:
                run.main()
            load.assert_called_once_with(Path('/private/config.env'))
            self.assertEqual(execute.call_args.args[1], [run.sys.executable, str(run.ROOT / script)])

    def test_both_install_variants_schedule_promptly_without_overlapping_oneshots(self):
        system = services.render('tahor', Path('/etc/tahor/config.env'), Path('/var/lib/tahor'), Path('/opt/tahor/venv/bin/python'), Path('/opt/tahor'))
        with tempfile.TemporaryDirectory() as directory:
            setup_tahor.write_units(Path(directory), Path('/private/config.env'), Path('/usr/bin/python3'))
            user = {path.name: path.read_text() for path in Path(directory).iterdir()}
        for units in (system, user):
            for name, command in [('tahor-subscriptions', 'subscriptions'), ('tahor-subscription-actions', 'subscription-actions')]:
                timer, service = units[name + '.timer'], units[name + '.service']
                for setting in ('OnBootSec=15s', 'OnUnitInactiveSec=15s', 'AccuracySec=1s'):
                    self.assertIn(setting, timer)
                self.assertNotIn('tahor-decisions.service', timer)
                self.assertIn('Type=oneshot', service)
                self.assertIn('TimeoutStartSec=300', service)
                self.assertIn('NoNewPrivileges=true', service)
                self.assertIn(' ' + command + ' ', service)
                self.assertNotIn('Restart=always', service)
        self.assertIn('User=tahor', system['tahor-subscriptions.service'])
        self.assertIn('ProtectSystem=strict', system['tahor-subscription-actions.service'])
