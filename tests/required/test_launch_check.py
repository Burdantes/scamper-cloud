from __future__ import annotations

import argparse
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from controller import launch_check, monthly, submit
from providers.azure import driver as azure
from providers.aws import driver as aws


def azure_catalog():
    return {name: NS(registration_state='Registered', resource_types=[NS(resource_type=r, locations=['West India']) for r in resources])
            for name, resources in {'Microsoft.Network': ['publicIPAddresses', 'virtualNetworks', 'networkInterfaces', 'networkSecurityGroups'],
                                    'Microsoft.Compute': ['virtualMachines', 'disks']}.items()}


def sku(size='Standard_B2s', restrictions=None):
    return NS(resource_type='virtualMachines', name=size, locations=['westindia'], restrictions=restrictions or [])


def test_azure_checks_resource_types_and_exact_size():
    providers = azure_catalog()
    assert launch_check.azure_region_errors('westindia', 'Standard_B2s', providers, [sku()]) == []
    providers['Microsoft.Network'].resource_types[0].locations = []
    errors = launch_check.azure_region_errors('westindia', 'Standard_B2ts_v2', providers, [sku()])
    assert any('publicIPAddresses' in error for error in errors)
    assert any('Standard_B2ts_v2' in error for error in errors)


def test_azure_subscription_restriction_blocks_location_not_unselected_zone():
    restricted = sku(restrictions=[NS(type='Location', values=['westindia'])])
    assert launch_check.azure_region_errors('westindia', restricted.name, azure_catalog(), [restricted])
    zonal = sku(restrictions=[NS(type='Zone', values=['westindia'])])
    assert not launch_check.azure_region_errors('westindia', zonal.name, azure_catalog(), [zonal])


def test_azure_region_override_reaches_vm_consumer(monkeypatch):
    monkeypatch.setenv('SCAMPER_AZR_VM_SIZES_JSON', '{"westindia":"Standard_B2s"}')
    assert azure.worker_size('westindia') == 'Standard_B2s'
    assert azure.worker_size('eastus') == azure.settings.AZR_VM_SIZE


def test_aws_skips_zone_without_default_public_subnet(monkeypatch):
    client = NS(describe_availability_zones=lambda **_: {'AvailabilityZones': [{'ZoneName':'uae-a'}, {'ZoneName':'uae-b'}]},
                describe_subnets=lambda **_: {'Subnets':[{'AvailabilityZone':'uae-a', 'MapPublicIpOnLaunch':True}]})
    monkeypatch.setattr(aws, 'ec2_client', lambda _: client)
    monkeypatch.setattr(aws, 'get_default_vpc', lambda _: 'vpc-1')
    assert aws.get_zones('me-central-1') == ['uae-a']


def test_eligible_regions_treats_regional_errors_as_partial_coverage():
    regions = ['east', 'west', 'central']
    assert launch_check.eligible_regions(
        regions, ['west: unavailable', 'central: size restricted']
    ) == ['east']
    assert launch_check.eligible_regions(
        regions, ['credential exchange failed']
    ) == []


def test_job_overrides_are_applied_after_wrapper_defaults():
    args = argparse.Namespace(provider='gcp',run_id='repair', worker_machine_type='e2-micro',
                              worker_image_project='debian-cloud', worker_image_family='debian-12', worker_image='debian-12-fixed')
    command = submit.systemd_command(args, ['python','driver.py'])
    wrapper = command.index('/usr/local/bin/scamper-controller-run')
    assert command[wrapper+1] == '/usr/bin/env'
    assert 'GCP_IMAGE=debian-12-fixed' in command[wrapper+2:]
    assert 'GCP_MACHINE_TYPE=e2-micro' in command[wrapper+2:]


def test_launch_timeout_is_a_readiness_failure(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 600
        raise subprocess.TimeoutExpired(args[0], 600)
    monkeypatch.setattr(monthly.subprocess, 'run', timeout)
    provider = monthly.ProviderSchedule('gcp', ('us-central1',), 'e2-micro', 1, None, None, 777600)
    report = monthly._launch_readiness(provider)
    assert report['ready'] is False
    assert 'timed out' in report['errors'][0]


def test_gcp_resolves_image_before_any_regional_requests(monkeypatch):
    import googleapiclient.discovery
    import providers.gcs_credentials
    calls=[]
    class Images:
        def getFromFamily(self, **kwargs):
            calls.append(kwargs)
            return NS(execute=lambda: {'name':'debian-12-fixed', 'status':'READY'})
    class Zones:
        def list(self, **kwargs):
            return NS(execute=lambda:{'items':[{'name':'us-central1-a','region':'regions/us-central1','status':'UP'}]})
        def list_next(self, **kwargs): return None
    client=NS(images=lambda: Images(), zones=lambda: Zones(), machineTypes=lambda:NS(get=lambda **_:NS(execute=lambda:{})))
    monkeypatch.setattr(googleapiclient.discovery,'build',lambda *a,**k:client)
    monkeypatch.setattr(providers.gcs_credentials,'google_credentials',lambda:None)
    monkeypatch.setattr(aws.settings,'GCP_IMAGE','')
    result=launch_check.check_gcp({'regions':['us-central1'],'worker_image_project':'debian-cloud','worker_image_family':'debian-12','worker_machine_type':'e2-micro'})
    assert calls == [{'project':'debian-cloud','family':'debian-12'}]
    assert result['worker_image'] == 'debian-12-fixed'
    assert result['errors'] == []


def test_azure_pages_only_requested_regions_and_sizes(monkeypatch):
    import azure.mgmt.compute
    calls=[]
    def list_skus(**kwargs):
        calls.append(kwargs)
        return iter([sku(),sku('irrelevant-size')])
    compute=NS(resource_skus=NS(list=list_skus),virtual_machine_images=NS(get=lambda *a:NS(name='22.04.20260901'), list=lambda *a:[NS(name='22.04.20260901')]))
    monkeypatch.setattr(azure.mgmt.compute,'ComputeManagementClient',lambda *a,**k:compute)
    # Import via the module since the test's name is shadowed by azure's SDK.
    from providers.azure import driver
    monkeypatch.setattr(driver,'get_resource_client',lambda:NS(providers=NS(get=lambda name:azure_catalog()[name])))
    monkeypatch.setattr(driver,'get_azure_credential',lambda:None)
    monkeypatch.setattr(driver,'get_subscription_id',lambda:'subscription')
    result=launch_check.check_azure({'regions':['westindia'],'worker_machine_type':'Standard_B2s'})
    assert result['errors']==[]
    assert calls==[{'filter':"location eq 'westindia'"}]


def test_azure_latest_is_resolved_numerically_before_get(monkeypatch):
    import azure.mgmt.compute
    from providers.azure import driver
    requested=[]
    def get(*args):
        requested.append(args[-1])
        assert args[-1] != 'latest'
    images=NS(list=lambda *a:[NS(name='22.4.9'),NS(name='22.4.10')],get=get)
    compute=NS(resource_skus=NS(list=lambda **_:iter([sku()])),virtual_machine_images=images)
    monkeypatch.setattr(azure.mgmt.compute,'ComputeManagementClient',lambda *a,**k:compute)
    monkeypatch.setattr(driver,'get_resource_client',lambda:NS(providers=NS(get=lambda name:azure_catalog()[name])))
    monkeypatch.setattr(driver,'get_azure_credential',lambda:None)
    monkeypatch.setattr(driver,'get_subscription_id',lambda:'subscription')
    result=launch_check.check_azure({'regions':['westindia'],'worker_machine_type':'Standard_B2s'})
    assert result['errors']==[]
    assert requested==['22.4.10']
    assert result['image_versions']=={'westindia':'22.4.10'}
    monkeypatch.setenv('SCAMPER_AZR_IMAGE_VERSIONS_JSON',json.dumps(result['image_versions']))
    assert driver.worker_image_version('westindia')=='22.4.10'


def test_azure_normalizes_sku_locations_and_restrictions():
    mixed = sku()
    mixed.locations = ['WestIndia']
    assert launch_check.azure_region_errors('westindia', mixed.name, azure_catalog(), [mixed]) == []
    mixed.restrictions = [NS(type='Location', values=['WestIndia'])]
    assert launch_check.azure_region_errors('westindia', mixed.name, azure_catalog(), [mixed])


def test_azure_mapping_restrictions_do_not_confuse_values_method():
    restricted = sku(restrictions=[{'type': 'Location', 'values': ['WestIndia']}])
    assert launch_check.azure_region_errors('westindia', restricted.name, azure_catalog(), [restricted])
    restricted.restrictions = [{'type': 'Location', 'values': ['EastUS']}]
    assert launch_check.azure_region_errors('westindia', restricted.name, azure_catalog(), [restricted]) == []


def test_gcp_region_override_reaches_worker(monkeypatch):
    from providers.gcp import driver
    monkeypatch.setenv('SCAMPER_GCP_MACHINE_TYPES_JSON', '{"asia-southeast3":"n4-standard-2"}')
    assert driver.worker_machine_type('asia-southeast3-a') == 'n4-standard-2'
    assert driver.worker_machine_type('us-central1-a') == driver.settings.GCP_MACHINE_TYPE
    args = argparse.Namespace(provider='gcp',run_id='repair',worker_machine_type='e2-micro',worker_machine_types_json='{"asia-southeast3":"n4-standard-2"}')
    assert any('SCAMPER_GCP_MACHINE_TYPES_JSON=' in value for value in submit.systemd_command(args, ['python', 'driver.py']))


def test_gcp_n4_request_uses_compatible_disk_and_network(monkeypatch, tmp_path):
    from providers.gcp import driver
    bodies=[]
    images=NS(getFromFamily=lambda **_: NS(execute=lambda:{'selfLink':'image'}))
    def insert(**kwargs):
        bodies.append(kwargs['body'])
        return NS(execute=lambda:{})
    monkeypatch.setattr(driver,'get_compute',lambda:NS(images=lambda:images,instances=lambda:NS(insert=insert)))
    monkeypatch.setattr(driver.settings,'GCP_IMAGE','')
    monkeypatch.setattr(driver.settings,'GCP_SCAMPER_SSH_KEY',str(tmp_path/'missing'))
    monkeypatch.setenv('SCAMPER_GCP_MACHINE_TYPES_JSON','{"asia-southeast3":"n4-standard-2"}')
    driver.create_instance('project','asia-southeast3-a','test')
    assert bodies[0]['disks'][0]['initializeParams']['diskType'].endswith('/hyperdisk-balanced')
    assert bodies[0]['disks'][0]['interface']=='NVME'
    assert bodies[0]['networkInterfaces'][0]['nicType']=='GVNIC'
