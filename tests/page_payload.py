import json
import re


def json_script_payload(html, script_id):
    match = re.search(
        rf'<script id="{re.escape(script_id)}" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing json_script {script_id}"
    return json.loads(match.group(1))
