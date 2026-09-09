# Scheduled start/stop

The instance runs only during market hours. A Lambda on an EventBridge schedule
starts and stops the machine; the machine itself starts and stops the bot.

```
03:30 UTC / 09:00 IST   Lambda  -> StartInstances
                        boot    -> cron @reboot -> start_bot.sh -> tmux fno-main, fno-oi
10:05 UTC / 15:35 IST   cron    -> stop_bot.sh  -> SIGTERM, WAL checkpoint
10:15 UTC / 15:45 IST   Lambda  -> StopInstances
```

**The ten-minute gap between 15:35 and 15:45 is the point of the design.** An
instance stop is not a clean unmount. Without a graceful shutdown first, SQLite
is killed mid-write and the last minutes of the session are the part most likely
to be missing — which is exactly the part nobody notices is gone.

## 1. Lambda

Runtime **Python 3.12**, handler `lambda_ec2_scheduler.lambda_handler`, timeout
**30s**. `boto3` is already in the runtime; no packaging needed.

**Environment variables**

| Key | Value |
|-----|-------|
| `INSTANCE_ID` | `i-0123456789abcdef0` |
| `HOLIDAYS` | `2026-10-20,2026-11-05` (optional, IST dates, comma-separated) |

**Execution-role policy** — scope it to the one instance, not `*`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ec2:StartInstances", "ec2:StopInstances"],
      "Resource": "arn:aws:ec2:ap-south-1:<ACCOUNT_ID>:instance/<INSTANCE_ID>"
    },
    {
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    }
  ]
}
```

`DescribeInstances` cannot be scoped to a resource — that is an AWS limitation,
not an oversight. Keep the write actions narrow; that is where the risk is.

## 2. EventBridge rules

Two rules, same target, different **constant JSON** input. The function refuses
to run without one, so a rule missing its input fails immediately and visibly
rather than picking a default and stopping your instance at 09:00.

| Rule | Schedule (UTC) | IST | Constant input |
|------|----------------|-----|----------------|
| `fno-bot-start` | `cron(30 3 ? * MON-FRI *)` | 09:00 | `{"action": "start"}` |
| `fno-bot-stop` | `cron(15 10 ? * MON-FRI *)` | 15:45 | `{"action": "stop"}` |

> **EventBridge cron has no timezone.** These are UTC and assume IST = UTC+5:30
> year round, which India observes — no DST to track.

## 3. On the instance

```bash
cd ~/cursor_FNO
chmod +x deploy/start_bot.sh deploy/stop_bot.sh
./deploy/start_bot.sh          # run it directly, not with `sh` -- it needs bash
sudo timedatectl set-timezone Asia/Kolkata     # so cron times mean IST
sudo yum install -y tmux sqlite                # if not already present
```

Then `crontab -e`:

```cron
@reboot        sleep 20 && /home/ec2-user/cursor_FNO/deploy/start_bot.sh
35 15 * * 1-5  /home/ec2-user/cursor_FNO/deploy/stop_bot.sh
```

The `sleep 20` gives the network stack time to come up. `start_bot.sh` also
polls for reachability, so this is belt-and-braces — boot ordering is the kind
of thing that works on every test and fails on the morning you stop watching.

### If it cannot find your interpreter

`start_bot.sh` looks for `bin/python3` then `bin/python` under, in order:
`$VENV` (if set), `$APP_DIR/venv`, `$APP_DIR/.venv`, `~/venv`, `~/.venv`, and
finally a system `python3`. A system interpreter is used only with a loud
warning, because it works solely if dependencies were installed globally.

If yours is somewhere else, find it and pass it in:

```bash
ls -d ~/*/bin/python3 ~/*/*/bin/python3 2>/dev/null
# or, if the venv is currently active:
which python3
```

Then set it in the crontab line rather than editing the script:

```cron
@reboot  sleep 20 && VENV=/home/ec2-user/myenv /home/ec2-user/cursor_FNO/deploy/start_bot.sh
```

## 4. Logs

One file per day per process, under `~/cursor_FNO/logs/`:

```
main_2026-09-09.log     main.py
oi_2026-09-09.log       oi_collector.py
boot_2026-09-09.log     start/stop script output
```

Dated rather than size-rotated: a dated file is trivially greppable after the
fact and never truncates the morning while you are reading the afternoon.

Nothing prunes them. At roughly 10 MB/day that is ~3.6 GB/year, so add this to
cron if disk gets tight:

```cron
0 16 * * 1-5  find /home/ec2-user/cursor_FNO/logs -name '*.log' -mtime +60 -delete
```

## 5. Verify it works

Do this on a **non-trading day** first, so a mistake costs nothing:

```bash
# Force a start, then watch the instance come up and the bot with it
aws lambda invoke --function-name fno-bot-scheduler \
  --payload '{"action":"start"}' --cli-binary-format raw-in-base64-out /tmp/out.json
cat /tmp/out.json

# Then, on the instance:
tmux ls                                   # expect fno-main and fno-oi
tail -f ~/cursor_FNO/logs/boot_$(date +%F).log
```

Both scripts are idempotent — running `start_bot.sh` twice will not produce two
bots — so re-running during debugging is safe.

## What this does and does not cover

**Covers:** the machine being up only during market hours, the bot starting with
it, a clean database shutdown, and per-day logs.

**Does not cover:** a process that dies at 11:30. Nothing restarts it, and the
next signal is a quiet log. If that turns out to happen, the fix is a systemd
unit with `Restart=always` in place of the tmux launch — the tmux approach was
chosen here because you can attach to a live session and watch, which is worth
more than automatic restart while the project is still being understood.
