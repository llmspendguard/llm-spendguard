"""Prove reviewed offline gates reject missing evidence and changed exceptions."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from spendguard.capability_readiness import load_capability_readiness_manifest
from spendguard.external_standard_readiness import assess_external_standard_subset

EXTERNAL_MUTATION_REPOSITORY = Path(__file__).resolve().parents[1]
EXTERNAL_MUTATION_CHANGED_BYTES = b"changed backend archive"
EXTERNAL_MUTATION_WORKFLOW_NAME = 'insecure.yaml'
EXTERNAL_MUTATION_UNPINNED_REFERENCE = 'owner/action@mutable-tag'
EXTERNAL_MUTATION_WORKFLOW_CONTENT = 'permissions:\n  contents: read\non: push\njobs:\n  probe:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: '
EXTERNAL_MUTATION_HEADING_PREFIX = '## '


def verify_external_standard_mutation_rejection():
    """Exercise real workflow parsing and byte-level waiver enforcement."""
    items = {item.id: item for item in load_capability_readiness_manifest()[1] if item.standard}
    assert len(items) == len({item.arguments['kind'] for item in items.values()})
    for item in items.values():
        assert item.gate and item.arguments['reviews']
        assert all(review['reason'] and review['tracking'] for review in item.arguments['reviews'])
    arguments = deepcopy(items['openssf-scorecard'].arguments)
    policy = arguments['policy']
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        paths = set(policy['required_files'])
        paths.update(str(path.relative_to(EXTERNAL_MUTATION_REPOSITORY))
                     for pattern in policy['workflows'] for path in EXTERNAL_MUTATION_REPOSITORY.glob(pattern))
        paths.update(waiver['path'] for waiver in policy['binary_waivers'])
        paths.update(waiver['document'] for waiver in policy['binary_waivers'])
        for relative in paths:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(EXTERNAL_MUTATION_REPOSITORY / relative, destination)
        subprocess.run(['git', 'init', str(root)], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(root), 'add', *sorted(paths)], capture_output=True, check=True)
        assert assess_external_standard_subset(context={'repo_root': root}, **arguments).status == 'PASS'
        artifact = root / policy['binary_waivers'][0]['path']
        original = artifact.read_bytes()
        artifact.write_bytes(EXTERNAL_MUTATION_CHANGED_BYTES)
        try:
            assess_external_standard_subset(context={'repo_root': root}, **arguments)
        except ValueError as error:
            assert 'waiver mismatch' in str(error)
        else:
            raise AssertionError('Changed binary passed its waiver')
        artifact.write_bytes(original)
        workflow_directory = next(path.parent for path in root.glob(policy['workflows'][0]))
        insecure = workflow_directory / EXTERNAL_MUTATION_WORKFLOW_NAME
        insecure.write_text(EXTERNAL_MUTATION_WORKFLOW_CONTENT + EXTERNAL_MUTATION_UNPINNED_REFERENCE)
        try:
            assess_external_standard_subset(context={'repo_root': root}, **arguments)
        except ValueError as error:
            assert 'not SHA pinned' in str(error)
        else:
            raise AssertionError('Unpinned Action in .yaml workflow passed')
        insecure.unlink()
        with patch('spendguard.external_standard_readiness.Path.glob', return_value=iter(())):
            try:
                assess_external_standard_subset(context={'repo_root': root}, **arguments)
            except ValueError as error:
                assert 'No workflows' in str(error)
            else:
                raise AssertionError('Missing workflow evidence passed')
        documentation = deepcopy(items['diataxis'].arguments)
        for mapping in documentation['policy']['quadrants'].values():
            for relative in {mapping['document'], *mapping['indexes']}:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(EXTERNAL_MUTATION_REPOSITORY / relative, destination)
        assert assess_external_standard_subset(context={'repo_root': root}, **documentation).status == 'PASS'
        tutorial = documentation['policy']['quadrants']['tutorial']
        tutorial_path = root / tutorial['document']
        tutorial_path.write_text(tutorial_path.read_text().replace(EXTERNAL_MUTATION_HEADING_PREFIX + tutorial['heading'], tutorial['heading']))
        try:
            assess_external_standard_subset(context={'repo_root': root}, **documentation)
        except ValueError as error:
            assert 'target heading' in str(error)
        else:
            raise AssertionError('Missing tutorial heading passed via stale navigation link')


if __name__ == '__main__':
    verify_external_standard_mutation_rejection()
    print('external readiness: review completeness, workflow presence and binary mutation enforced')
