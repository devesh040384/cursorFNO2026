"""Start and stop the trading EC2 instance on a weekday schedule.

One function, two EventBridge rules. The rule supplies {"action": "start"} or
{"action": "stop"}; nothing else decides which way it runs, so a misconfigured
rule fails loudly instead of guessing.

WHY A HOLIDAY LIST. EventBridge cron can express "weekdays" but not "NSE
trading days". Running on Diwali costs a few rupees of EC2 time and writes a
session of out-of-hours option-chain rows, which the in_session guard flags but
which still have to be reasoned about later. A plain list of dates is cheap to
maintain and obvious when wrong.

The stop path does NOT gracefully close the bot. That is deliberate: the EC2
instance runs its own pre-stop hook at 15:35 IST (see stop_bot.sh), fifteen
minutes before this function stops the machine. Two systems each doing half the
job, in a fixed order, beats one system trying to do both across a network
boundary it cannot see the result of.

Environment:
    INSTANCE_ID   required, e.g. i-0123456789abcdef0
    HOLIDAYS      optional, comma-separated YYYY-MM-DD, IST. Start is skipped
                  on these dates; stop always runs.

IAM (attach to the function role, scoped to the one instance):
    ec2:StartInstances, ec2:StopInstances, ec2:DescribeInstances

EventBridge (both cron expressions are UTC; IST is UTC+5:30):
    start   cron(30 3 ? * MON-FRI *)    ->  09:00 IST, constant input {"action":"start"}
    stop    cron(15 10 ? * MON-FRI *)   ->  15:45 IST, constant input {"action":"stop"}
"""
import datetime
import os

import boto3

ec2 = boto3.client("ec2")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# States from which the requested transition is a no-op or already under way.
# Calling StartInstances on a pending instance is harmless but noisy; skipping
# keeps the logs readable, which is the only way anyone notices a real problem.
ALREADY_STARTING = ("pending", "running")
ALREADY_STOPPING = ("stopping", "stopped", "shutting-down", "terminated")


def _instance_state(instance_id):
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            return inst.get("State", {}).get("Name")
    raise RuntimeError("instance %s not found" % instance_id)


def _holidays():
    raw = os.environ.get("HOLIDAYS", "")
    return {d.strip() for d in raw.split(",") if d.strip()}


def lambda_handler(event, context):
    instance_id = os.environ["INSTANCE_ID"]
    action = (event or {}).get("action")
    if action not in ("start", "stop"):
        # Not a retryable condition -- the rule is wrong, and retrying a wrong
        # rule every minute is worse than failing once.
        raise ValueError("event must carry action=start|stop, got %r" % (action,))

    today = datetime.datetime.now(IST).date().isoformat()

    if action == "start" and today in _holidays():
        print("SKIP start: %s is in HOLIDAYS" % today)
        return {"action": action, "instance": instance_id, "result": "skipped_holiday"}

    state = _instance_state(instance_id)

    if action == "start":
        if state in ALREADY_STARTING:
            print("SKIP start: %s already %s" % (instance_id, state))
            return {"action": action, "instance": instance_id, "result": "noop", "state": state}
        ec2.start_instances(InstanceIds=[instance_id])
        print("START issued for %s (was %s)" % (instance_id, state))
    else:
        if state in ALREADY_STOPPING:
            print("SKIP stop: %s already %s" % (instance_id, state))
            return {"action": action, "instance": instance_id, "result": "noop", "state": state}
        ec2.stop_instances(InstanceIds=[instance_id])
        print("STOP issued for %s (was %s)" % (instance_id, state))

    return {"action": action, "instance": instance_id, "result": "issued", "was": state}
