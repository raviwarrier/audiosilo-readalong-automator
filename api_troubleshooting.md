# Troubleshooting: Cloudflare Blocking AudioSilo API Requests

## Status

**Cloudflare interception resolved. Actual read-along job submission must still be verified.**

After applying the fixes below, `queue_readalongs.py` can reach the AudioSilo API, but a successful end-to-end job submission (a job ID returned and visible in AudioSilo) has not yet been confirmed.

---

## Problem

The `queue_readalongs.py` script failed when submitting read-along jobs to AudioSilo.

The error initially appeared as:

```
FAILED (stays NEW): Connection error: Forbidden
```

Both user API keys and administrator API keys produced the same result.

The script was submitting jobs to:

```
POST /api/v1/admin/bridge/jobs
```

### Root Cause

Cloudflare was blocking Python HTTP requests before they reached the AudioSilo server.

Two Cloudflare security features were identified as blockers:

1. **AI Labyrinth:** Initially intercepted the request and returned Cloudflare error 1010.
2. **Browser Integrity Check:** Continued blocking requests after AI Labyrinth was disabled.

The Cloudflare Security Events dashboard confirmed both blocks. The requests originated from the Raspberry Pi and used the `Python-urllib/3.13` User-Agent.

This was a Cloudflare security issue, **not** an AudioSilo API-key or server authentication problem.

---

## Fixes

### Fix 1 — Disable AI Labyrinth (essential)

In the Cloudflare dashboard:

1. Select the `raviwarrier.net` domain.
2. Navigate to **Security → Settings**.
3. Find **AI Labyrinth** under Bot Traffic settings.
4. Switch it **OFF**.

AI Labyrinth does not offer hostname or URL-path exclusions in its settings.

### Fix 2 — Create a Custom Rule to Skip Browser Integrity Check (essential)

In Cloudflare:

1. Navigate to **Security → Security rules → Custom rules**.
2. Create a new rule named:

   `Allow AudioSilo API`
3. Use this expression:

```
(http.host eq "abooks.raviwarrier.net" and starts_with(http.request.uri.path, "/api/v1/"))
```

4. Set the action to **Skip**.
5. Under **WAF components to skip**, select only:
   - Browser Integrity Check
6. Leave all other options unchecked.
7. Enable **Log matching requests**.
8. Save and deploy the rule.

This exempts AudioSilo API requests from Browser Integrity Check without disabling the feature for other websites.

Note: The rule currently covers all `/api/v1/` endpoints on the AudioSilo hostname. It can be narrowed to the specific job-submission endpoint if required.

### Fix 3 — Correct HTTP Error Handling in Python (essential)

In `queue_readalongs.py`, the original exception handlers caught `URLError` before `HTTPError`.

Since `HTTPError` is a subclass of `URLError`, HTTP responses such as 403 were incorrectly reported as connection errors.

The handlers were reordered so that `HTTPError` is caught first:

```
    except socket.timeout as exc:
        raise APITimeoutError(f"Request timed out after {timeout}s") from exc
    except urllib.error.HTTPError as exc:
        # HTTPError is a subclass of URLError, so it must be caught first.
        body = exc.read().decode("utf-8", errors="replace")
        raise APIError(f"HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        # Wrap connection-level errors (reason may be socket.timeout too).
        if isinstance(exc.reason, socket.timeout):
            raise APITimeoutError(f"Request timed out after {timeout}s") from exc
        raise APIError(f"Connection error: {exc.reason}") from exc
```

This ensures that HTTP errors display their actual status code and response body instead of being masked as connection errors.

### Fix 4 — Add an Explicit User-Agent (recommended)

In `queue_readalongs.py`, locate the HTTP request headers:

```
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {token}")
```

Add an explicit User-Agent:

```
req.add_header("User-Agent", "AudioSilo-ReadAlong-Queue/1.0")
```

The complete header section becomes:

```
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {token}")
req.add_header("User-Agent", "AudioSilo-ReadAlong-Queue/1.0")
```

This identifies the client explicitly to Cloudflare and other edge protections instead of using the default `Python-urllib/3.x` user agent.

---

## Verification

The following test demonstrated that the request could reach AudioSilo after the Cloudflare changes:

```
curl -i -X POST \
  "https://abooks.raviwarrier.net/api/v1/admin/bridge/jobs" \
  -H "Authorization: Bearer YOUR_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -H "User-Agent: Python-urllib/3.13" \
  -d '{"kind":"readalong.align","library_id":"test","path":"test","params":{}}'
```

Expected diagnostic result:

```
HTTP/2 400
{"error":"invalid request"}
```

This response is expected because the test deliberately uses an invalid library ID and path. It confirms that the request reached the AudioSilo application rather than being blocked by Cloudflare.

---

## Important Security Note

API keys used during troubleshooting were exposed in terminal output shared during the investigation.

**Revoke those keys and generate a replacement administrator API key.** Never store API keys directly in the script or commit them to Git. API keys belong only in the local, git-ignored `.env` file.

---

## Final Submission Test (unverified)

After applying all fixes:

1. Run `python queue_readalongs.py`.
2. Select exactly one title.
3. Confirm submission.
4. Verify that AudioSilo accepts the job and returns a job ID.
5. Check that the job appears in AudioSilo.
6. Only after successful verification should larger batches be submitted.

This end-to-end submission has **not yet been verified**.
