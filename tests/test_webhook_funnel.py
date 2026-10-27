"""Webhooks use shared public :443 while private Serve stays on :8443."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from scripts import private_access as pa

HOST="devstack.example.ts.net"

def status(funnel=False,mcp=False,legacy=False):
    private="443" if legacy else "8443"
    tcp={private:{"HTTPS":True}}
    web={HOST+":"+private:{"Handlers":{"/":{"Proxy":pa.LOCAL_TARGET}}}}
    allow={}
    if legacy:
        if funnel:
            tcp["8443"]={"HTTPS":True}
            web[HOST+":8443"]={"Handlers":{"/":{"Proxy":"http://127.0.0.1:8767"}}}
            allow[HOST+":8443"]=True
        if mcp:
            tcp["10000"]={"HTTPS":True}
            web[HOST+":10000"]={"Handlers":{"/":{"Proxy":"http://127.0.0.1:8768"}}}
            allow[HOST+":10000"]=True
    elif funnel or mcp:
        handlers={}
        if funnel:
            handlers["/hooks/"]={"Proxy":"http://127.0.0.1:8767/hooks/"}
        if mcp:
            handlers["/mcp"]={"Proxy":"http://127.0.0.1:8768/mcp"}
        tcp["443"]={"HTTPS":True}
        web[HOST+":443"]={"Handlers":handlers}
        allow[HOST+":443"]=True
    return json.dumps({"TCP":tcp,"Web":web,"AllowFunnel":allow})

def connected():
    return json.dumps({"BackendState":"Running","Self":{"DNSName":HOST+".","ID":"n1"},
        "CurrentTailnet":{"Name":"example.ts.net"}})

@contextmanager
def funnel():
    path=Path(__file__).resolve().parents[1]/"scripts"/"webhook_funnel.py"
    spec=importlib.util.spec_from_file_location("webhook_funnel_test",path)
    module=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{"private_access":pa}):
        spec.loader.exec_module(module)
    yield module

class FunnelTests(unittest.TestCase):
    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_already_enabled_does_not_modify_routes(self,cli,pin):
        cli.side_effect=[connected(),status(funnel=True,mcp=True)]
        with funnel() as helper:
            self.assertIn("Webhooks public",helper.configure())
        self.assertEqual(cli.call_count,2)

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_disable_webhook_keeps_public_mcp_and_private_dashboard(self,cli,pin):
        cli.side_effect=[connected(),status(funnel=True,mcp=True),"",status(mcp=True)]
        with funnel() as helper:
            self.assertIn("disabled",helper.configure(disable=True))
        self.assertEqual(cli.call_args_list[2].args,
                         ("funnel","--https=443","--set-path=/hooks/","off"))

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_disable_legacy_webhook_without_touching_private_or_mcp(self,cli,pin):
        cli.side_effect=[connected(),status(funnel=True,mcp=True,legacy=True),
                         "",status(mcp=True,legacy=True)]
        with funnel() as helper:
            self.assertIn("disabled",helper.configure(disable=True))
        self.assertEqual(cli.call_args_list[2].args,("funnel","--https=8443","off"))

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_legacy_cannot_publish_new_callback(self,cli,pin):
        cli.side_effect=[connected(),status(legacy=True)]
        with funnel() as helper,self.assertRaisesRegex(pa.PrivateAccessError,"Migrate"):
            helper.configure()
        self.assertEqual(cli.call_count,2)

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    def test_refuses_foreign_existing_serve(self,cli,pin):
        cli.side_effect=[connected(),'{"TCP":{"443":{"HTTPS":true}},"Web":{}}']
        with funnel() as helper,self.assertRaises(pa.PrivateAccessError):
            helper.configure()
        self.assertEqual(cli.call_count,2)

    @patch("scripts.private_access.pin_initial_identity")
    @patch("scripts.private_access.run_cli")
    @patch("urllib.request.urlopen")
    def test_enables_path_scoped_public_webhooks_and_preserves_mcp(self,urlopen,cli,pin):
        urlopen.return_value.__enter__.return_value.read.return_value=b'{"scope":"webhook-only"}'
        cli.side_effect=[connected(),status(mcp=True),"",status(funnel=True,mcp=True)]
        with funnel() as helper:
            self.assertIn("/hooks/",helper.configure())
        self.assertEqual(cli.call_args_list[2].args,
                         ("funnel","--yes","--bg","--https=443",
                          "--set-path=/hooks/","http://127.0.0.1:8767/hooks/"))

if __name__=="__main__":
    unittest.main()
