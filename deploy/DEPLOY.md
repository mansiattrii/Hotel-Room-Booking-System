# Deploying to AWS EC2

A step-by-step guide to running the full stack (nginx, API, worker, PostgreSQL) on one EC2
instance. You run every step yourself; nothing here needs your AWS credentials anywhere
except the AWS console and your own terminal.

Commands marked **PowerShell** run on your Windows machine. Commands marked **server** run
on the EC2 instance over SSH.

**What was tested before writing this:** the Docker install commands on a clean Ubuntu
24.04 container, a fresh clone of the repository started with a production-style `.env`,
seeding the smaller dataset, the RDS override against a separate PostgreSQL, and the HTTPS
nginx config (`nginx -t`). Not tested: the AWS console steps themselves and issuing a real
Let's Encrypt certificate, which needs a domain.

---

## 0. Before you start: cost

- **Account created on or after 15 July 2025:** the AWS Free plan gives USD 100 in credits
  at sign-up (up to 100 more for completing activities), for 6 months or until the credits
  run out. Usage is deducted from the credits.
- **Account created before 15 July 2025:** the legacy Free Tier gives 750 hours a month of
  `t2.micro` or `t3.micro` for 12 months from sign-up. Beyond the limits, you pay normal rates.
- **Public IPv4 addresses are charged** (USD 0.005 per hour per address, about 3.60 a month).
  Legacy free tier accounts get 750 hours a month of public IPv4 on EC2 for the first year.
- AWS changes these offers. Check <https://aws.amazon.com/free/> and trust the
  **"Free tier eligible"** label in the console over this document.

Whatever your account type, follow section 9 (teardown) when you're done.

---

## 1. Create a billing alert first

1. Sign in to the AWS console as the root user or an admin user.
2. Open **Billing and Cost Management** → **Budgets** → **Create budget**.
3. Choose **Use a template** → **Zero spend budget**. Enter your email address. Create it.
   AWS emails you as soon as anything costs more than USD 0.01.
4. Optional, but useful: a second budget from the **Monthly cost budget** template, for
   example USD 5, so you hear about a slow leak and not only about the first cent.

Budget alerts arrive by email and can lag by several hours. They warn you; they don't stop
anything.

---

## 2. Create an SSH key pair

1. Pick one region for everything (top right of the console, for example
   **Asia Pacific (Mumbai) ap-south-1**). Resources in other regions are easy to forget.
2. **EC2** → **Key Pairs** → **Create key pair**. Name: `hotel-booking`, type **ED25519**,
   format **.pem**. The private key downloads once; AWS doesn't keep a copy.
3. Move it into your SSH folder and restrict who can read it. OpenSSH on Windows refuses a
   key that other users can read.

**PowerShell**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
Move-Item "$env:USERPROFILE\Downloads\hotel-booking.pem" "$env:USERPROFILE\.ssh\hotel-booking.pem"
icacls "$env:USERPROFILE\.ssh\hotel-booking.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

Never commit this file, paste it anywhere, or send it to anyone.

---

## 3. Launch the instance

**EC2** → **Instances** → **Launch instances**:

| Setting | Value |
|---|---|
| Name | `hotel-booking` |
| AMI | **Ubuntu Server 24.04 LTS**, 64-bit (x86), marked "Free tier eligible" |
| Instance type | See below |
| Key pair | `hotel-booking` |
| Storage | 20 GiB **gp3** |

**Instance type.** Pick one that the console marks **Free tier eligible** for your account:

- `t3.micro` (2 vCPU, 1 GiB memory): eligible for both account types. Enough: measured on
  a local rehearsal with the smaller dataset, the four containers used about 221 MiB in
  total after 200 concurrent searches. Section 4 adds swap so building the image has
  headroom.
- `t3.small` (2 GiB): eligible only for accounts created on or after 15 July 2025, and uses
  credits faster. Choose it if you have it and want more margin.

Stay on x86 (`t3`) rather than ARM (`t4g`): it matches what the image was built and tested on.

**Network settings** → **Edit**:

- **Auto-assign public IP:** Enable.
- **Create security group**, named `hotel-booking-sg`, with these inbound rules:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Only your current IP address can try to log in |
| HTTP | 80 | Anywhere (0.0.0.0/0) | The API, through nginx |
| HTTPS | 443 | Anywhere (0.0.0.0/0) | Only needed for the optional HTTPS section |

Don't add a rule for 5432. PostgreSQL is bound to the instance's loopback interface and must
never be reachable from the internet.

Click **Launch instance**. When its state is **Running** and status checks pass, copy its
**Public IPv4 address** from the instance details. Below it's written as `<PUBLIC-IP>`.

---

## 4. Connect and prepare the server

**PowerShell**

```powershell
ssh -i "$env:USERPROFILE\.ssh\hotel-booking.pem" ubuntu@<PUBLIC-IP>
```

Answer `yes` to the host key prompt the first time. Your prompt changes to
`ubuntu@ip-...`. Everything in this section runs there.

**Add 2 GiB of swap** (headroom while the image builds on a 1 GiB instance):

**server**

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

`free -h` should show `Swap: 2.0Gi`.

**Install Docker Engine and the Compose plugin** from Docker's own repository:

**server**

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
exit
```

`usermod` lets the `ubuntu` user run `docker` without `sudo`; it takes effect at the next
login, which is why the block ends with `exit`. Reconnect with the same `ssh` command, then:

**server**

```bash
docker --version
docker compose version
```

On a clean Ubuntu 24.04 these printed `Docker version 29.9.1` and
`Docker Compose version v5.6.0` when this guide was tested; newer is fine.

---

## 5. Get the code and create `.env`

**server**

```bash
git clone https://github.com/mansiattrii/Hotel-Room-Booking-System.git hotel-booking
cd hotel-booking
```

Create `.env` from the example, with freshly generated secrets. They're generated on the
server and never leave it:

**server**

```bash
PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
JWT=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
sed -e "s|^APP_ENV=.*|APP_ENV=production|" \
    -e "s|^JWT_SECRET=.*|JWT_SECRET=$JWT|" \
    -e "s|change-me|$PW|g" \
    .env.example > .env
unset PW JWT
chmod 600 .env
grep -E "^(APP_ENV|HTTP_PORT|BOOKING_STRATEGY)=" .env
```

The last command should print `APP_ENV=production`, `HTTP_PORT=80` and
`BOOKING_STRATEGY=constraint`.

- `token_urlsafe` produces URL-safe passwords. That matters: compose builds the database
  URL from `POSTGRES_PASSWORD`, so characters like `@` or `/` would break it.
- `APP_ENV=production` also makes the experiment tooling refuse to touch this database.
- To view or edit the file later: `nano .env`. Don't print it into anything you share.

---

## 6. Start the stack

**server**

```bash
docker compose up -d --build --wait
docker compose ps -a
```

The first run builds the image, so it takes a few minutes on a small instance. `--wait`
returns once every service is running or healthy. Expected `ps` output:

- `db`, `api`: `Up ... (healthy)`
- `worker`, `nginx`: `Up`
- `migrate`: `Exited (0)`

**Migrations run automatically.** The `migrate` service runs `alembic upgrade head` and
exits; the API and worker only start once it succeeded. To run them by hand (for example
after pulling new migrations without restarting): `docker compose run --rm migrate`.

---

## 7. Seed a smaller dataset and create an admin

The full dataset is for the benchmark. A demo needs far less:

**server**

```bash
docker compose run --rm api python -m scripts.seed --hotels 20 --rooms 1000 --bookings 10000 --users 500
docker compose run --rm api python -m scripts.create_admin --email <your-email>
```

`create_admin` prompts for a password; choose a strong one.

**Seeded users are public demo accounts.** They're `user1@example.com` ...
`user500@example.com`, all with the password `password123`. That's deliberate: anyone you
share the URL with can log in and try a booking. Don't reuse that password for anything,
and don't give demo users the admin role.

---

## 8. Check it from your machine

**PowerShell**

```powershell
curl.exe -i http://<PUBLIC-IP>/api/v1/health
```

Expected: `HTTP/1.1 200 OK`, `Server: nginx`, and `{"status":"ok","database":"ok"}`.

In a browser, open `http://<PUBLIC-IP>/docs` for the interactive API documentation.

**Useful commands on the server:**

```bash
docker compose logs -f api worker      # JSON log lines; Ctrl+C to stop following
docker compose ps -a
docker stats --no-stream               # memory per container
```

**Deploying a new version:**

```bash
cd ~/hotel-booking
git pull
docker compose up -d --build --wait
```

There's one API container, so it's briefly unavailable while it restarts.

**If something's wrong:**

| Symptom | Check |
|---|---|
| `ssh` times out | Your IP changed. Edit the security group's SSH rule back to **My IP**. |
| Browser can't connect at all | Security group has HTTP 80 open? `docker compose ps` shows nginx up? |
| `502 Bad Gateway` | `docker compose ps`: is `api` healthy? `docker compose logs api` |
| `migrate` exited with code 1 | `docker compose logs migrate` |
| Build killed, or the instance freezes | Memory: `free -h` should show the swap from section 4 |

---

## Optional: HTTPS with Let's Encrypt

Needs a domain name you control. Let's Encrypt won't issue a certificate for a bare IP
address or for `*.amazonaws.com` names. Not tested end to end for this project: the nginx
config was checked with `nginx -t` against a test certificate, but no real certificate was
issued.

1. **Give the instance a fixed IP.** EC2 → **Elastic IPs** → **Allocate**, then
   **Associate** it with the instance. Without this, the public IP changes every time the
   instance stops and starts. (An Elastic IP is a public IPv4 address and costs the same
   hourly rate; it keeps costing while allocated, even if the instance is stopped.)
2. **Point the domain at it:** at your DNS provider, create an `A` record, for example
   `api.yourdomain.com` → the Elastic IP. Wait until
   `nslookup api.yourdomain.com` returns that IP.
3. **Get a certificate** (nginx must be stopped so certbot can use port 80):

   **server**
   ```bash
   cd ~/hotel-booking
   docker compose stop nginx
   docker run --rm -p 80:80 -v /etc/letsencrypt:/etc/letsencrypt certbot/certbot certonly \
     --standalone -d api.yourdomain.com --email <your-email> --agree-tos --no-eff-email
   ```
4. **Put your domain into the HTTPS config** and start nginx with it:

   **server**
   ```bash
   sed -i "s/api.example.com/api.yourdomain.com/g" deploy/nginx-https.conf
   docker compose -f docker-compose.yml -f deploy/docker-compose.https.yml up -d --wait
   ```
   From now on, include both `-f` options in every `docker compose` command on this
   server. `http://` now redirects to `https://`.
5. **Renewal.** Certificates last 90 days. Renew monthly with a cron job (`crontab -e`):
   ```
   0 4 1 * * cd /home/ubuntu/hotel-booking && docker compose -f docker-compose.yml -f deploy/docker-compose.https.yml stop nginx; docker run --rm -p 80:80 -v /etc/letsencrypt:/etc/letsencrypt certbot/certbot renew; docker compose -f docker-compose.yml -f deploy/docker-compose.https.yml start nginx
   ```
   nginx is down for the few seconds certbot needs port 80. The `;` separators make sure
   nginx starts again even if the renewal fails.

Don't commit the edited `deploy/nginx-https.conf`; it's server-specific
(`git checkout deploy/nginx-https.conf` undoes the edit before a `git pull`).

---

## Optional: move PostgreSQL to RDS

**Cost warning first.** RDS is billed separately from EC2: instance hours, storage, backup
storage beyond the free allowance, and its own charges if you enable Multi-AZ (roughly
doubles the instance cost) or public access (another public IPv4 address). Check
eligibility in the console as with EC2, and delete the database when you're done
(section 9). For a portfolio demo, the PostgreSQL container on the EC2 instance is enough;
this section is here to show you know how the setup changes.

**1. Security group for the database.** EC2 → **Security Groups** → **Create**:
`hotel-booking-db-sg`, same VPC as the instance, one inbound rule:

| Type | Port | Source |
|---|---|---|
| PostgreSQL | 5432 | **Custom** → the security group `hotel-booking-sg` (not an IP range) |

Referencing the instance's security group means only that instance can connect, even if
its IP changes.

**2. Create the database.** **RDS** → **Create database**:

| Setting | Value |
|---|---|
| Creation method | Standard create |
| Engine | PostgreSQL, version **16.x** (the version this project is tested on) |
| Template | **Free tier** if offered, otherwise **Dev/Test** |
| Availability | **Single-AZ** |
| DB instance identifier | `hotel-booking-db` |
| Master username | `hotel` |
| Credentials | **Self managed**; generate the password on the server as in section 5 (URL-safe) |
| Instance class | a `db.t4g.micro` or `db.t3.micro` marked free tier eligible |
| Storage | 20 GiB gp3; turn **off** storage autoscaling |
| Connectivity | **Don't connect to an EC2 compute resource**; same VPC as the instance; **Public access: No**; security group `hotel-booking-db-sg` only (remove `default`) |
| Additional configuration | Initial database name: `hotel_booking` |

Creation takes several minutes. Then copy the **Endpoint** from the database's
**Connectivity & security** tab, something like
`hotel-booking-db.abc123xyz.ap-south-1.rds.amazonaws.com`.

**3. The connection string change.** Add one line to `.env` on the server:

```bash
RDS_DATABASE_URL=postgresql+psycopg://hotel:<RDS-PASSWORD>@<RDS-ENDPOINT>:5432/hotel_booking?sslmode=require
```

`sslmode=require` makes the driver refuse an unencrypted connection; RDS for PostgreSQL
supports TLS by default.

**4. Restart on RDS.** `deploy/docker-compose.rds.yml` points the API, worker and
migrations at `RDS_DATABASE_URL` and stops starting the local `db` container:

**server**

```bash
docker compose down
docker compose -f docker-compose.yml -f deploy/docker-compose.rds.yml up -d --wait
docker compose -f docker-compose.yml -f deploy/docker-compose.rds.yml run --rm api python -m scripts.seed --hotels 20 --rooms 1000 --bookings 10000 --users 500
```

The migrations create the schema on RDS, including `CREATE EXTENSION btree_gist`, which RDS
allows for the master user. This was tested locally against a separate PostgreSQL 16
container standing in for RDS: migrations reached `0003`, `btree_gist` was installed, and
`/health` passed through nginx with no `db` container running.

Use both `-f` options in every compose command from now on. The old `pgdata` volume on the
instance stays until you remove it (`docker volume rm hotel-booking_pgdata`).

---

## 9. Teardown: stop everything that bills

Do these in order, in the region you used, then check again the next day.

1. **RDS (if created):** RDS → Databases → `hotel-booking-db` → **Actions → Delete**.
   Untick **Create final snapshot**, tick the acknowledgement, and delete automated
   backups. Then RDS → **Snapshots**: delete any manual snapshots.
2. **EC2 instance:** EC2 → Instances → select it → **Instance state → Terminate**.
   *Stop* is not enough: a stopped instance still bills for its EBS volume (and an Elastic
   IP, if associated). Terminating deletes the root volume as well, by default.
3. **Elastic IP (if allocated):** EC2 → Elastic IPs → **Release Elastic IP address**. An
   allocated but unattached Elastic IP keeps billing.
4. **Leftover volumes and snapshots:** EC2 → **Volumes** and **Snapshots** should be empty
   (or contain nothing from this project).
5. **Optional cleanup:** security groups `hotel-booking-db-sg` and `hotel-booking-sg`, and
   the key pair (they don't cost anything). Delete the local key with
   `Remove-Item "$env:USERPROFILE\.ssh\hotel-booking.pem"` once the key pair is gone.
6. **Check every region:** EC2 → **EC2 Global View** shows instances, volumes and other
   resources across all regions.
7. **Check the bill:** **Billing and Cost Management** → **Bills** the next day. Charges
   appear with a delay of up to a day.

Keep the zero spend budget from section 1; it costs nothing and catches anything you missed.
