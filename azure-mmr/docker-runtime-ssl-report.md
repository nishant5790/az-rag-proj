# Docker Runtime SSL Investigation Report

## Summary

This report documents the investigation into the backend failure observed when running the FastAPI API through Docker Compose and testing the query `structure of barcode`.

The backend container now builds and starts successfully, but the live `/search` request still fails at runtime with an SSL certificate verification error when the container attempts to connect to Azure AI Search.

The final error returned by the backend was:

```json
{"detail":"Search backend error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1016)"}
```

## Goal

The goal of the test was to validate the backend end to end by:

1. Building the API container with Docker Compose.
2. Starting the FastAPI backend.
3. Confirming the backend health endpoint works.
4. Running the search query `structure of barcode` against the live backend.
5. Identifying exactly where the failure occurs.

## Relevant Components

The following files were involved in the behavior being tested:

- `api/main.py`
- `api/routes/search.py`
- `search/client.py`
- `config.py`
- `Dockerfile.api`
- `docker-compose.yml`
- `requirements-api.txt`

## Initial Findings

### 1. Application-level truststore support already existed

The backend code already included calls to `truststore.inject_into_ssl()` in application startup paths.

This showed that the code had already attempted to address certificate trust issues at the Python level.

### 2. The `truststore` package was not available in the API container dependency list

Even though the app attempted to import `truststore`, the package was not present in the API requirements originally. That meant the SSL injection logic could silently fail or be skipped depending on environment.

`truststore` was then added to the API dependency list so the existing code path could actually execute.

## Build-Time Failure

### What happened

The first Docker Compose build failed before the backend started.

The failure occurred during this Dockerfile step:

```dockerfile
RUN pip install --no-cache-dir -r requirements-api.txt
```

The error was an SSL certificate verification failure while `pip` tried to download Python packages from PyPI.

Representative error:

```text
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1016)'))
```

### Interpretation

This first failure was not related to Azure Search.

It was a build-time Python package installation problem inside the Linux image. The container could not validate the TLS certificate chain presented by PyPI from within the build environment.

### Why it happened

Inside Docker, the build runs in a Linux environment with its own certificate trust configuration. That environment does not automatically inherit the Windows trust store from the host machine.

If the network uses TLS interception or a private corporate CA, the container cannot validate certificates unless that CA is trusted inside the container.

### How the build was made to work

To get the image built in the current network, the Docker build path was adjusted to allow passing `pip` trusted hosts into the build.

This was a narrow build-path change only. It allowed `pip install` to complete successfully so the backend container could actually be started and tested.

## Runtime Test

After the build succeeded, the API container was started through Docker Compose.

### Startup result

The backend started successfully.

Observed behavior included:

- Uvicorn started on `http://0.0.0.0:8000`
- The FastAPI app completed startup
- The container stayed running
- The health endpoint responded successfully

### Health validation

The endpoint below was tested successfully:

```text
GET /healthz
```

The response was:

```json
{"status":"ok"}
```

This confirmed that:

- The container was running correctly.
- The FastAPI app loaded successfully.
- Routing was functional.
- The backend was reachable from the host.

## Query Test

The live backend was then tested using this request:

```json
{"query":"structure of barcode","top":5}
```

This request was sent to:

```text
POST /search
```

### Result

The backend returned:

```json
{"detail":"Search backend error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1016)"}
```

HTTP status:

```text
502 Bad Gateway
```

## What the Logs Confirmed

The container logs showed that the application did make an outbound request to Azure AI Search.

The request targeted:

```text
https://agent-ai-search5yc2.search.windows.net/indexes('pdg-was-multimodal-rag-1')/docs/search.post.search
```

The logs also showed:

- The request method was `POST`
- An `api-key` header was present
- A request body was sent

This is important because it proves the following:

1. The backend request flow reached the Azure Search client.
2. The app had enough configuration to construct the outbound Azure Search call.
3. The failure occurred during TLS validation of the HTTPS connection, not before request construction.

## Why This Is Not an Application Logic Bug

This failure is not consistent with a query-building bug or a routing bug.

If the problem were in application logic, the symptoms would likely have been different:

- A malformed request would more likely produce a `400`.
- A bad API key would more likely produce `401` or `403`.
- A bad index name would more likely produce `404`.
- A schema mismatch would usually surface as a service-side validation error.

Instead, the failure is:

```text
[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
```

That error occurs before the application can receive and process a normal service response.

## Why `truststore` Did Not Resolve the Runtime Problem

The logs showed:

```text
Truststore SSL injection successful.
```

This does not mean the container had the right CA certificates. It only means the Python runtime successfully switched to using the platform trust mechanism.

The key point is that the platform is the Linux container, not the Windows host.

Inside the running container:

- Python is using the container's trust configuration.
- If the required corporate or intercepting CA is not present in that Linux trust store, certificate validation still fails.

So the runtime SSL injection succeeded mechanically, but the necessary trusted issuer still was not available inside the container.

## Root Cause

The root cause is a missing trusted issuer certificate chain inside the running Linux container.

In practical terms:

- The container does not trust the certificate issuer chain presented for outbound HTTPS to Azure AI Search.
- Because of that, OpenSSL in Python rejects the connection.
- The Azure Search SDK raises an SSL verification exception.
- The FastAPI route catches the exception and returns `502 Bad Gateway`.

## Distinction Between the Two SSL Problems Observed

Two separate SSL failures occurred during the investigation.

### A. Build-time SSL failure

- Happened during `pip install`
- Target was PyPI
- Prevented the container image from building

### B. Runtime SSL failure

- Happened after the container started
- Target was Azure AI Search
- Prevented `/search` from returning results

These two failures are related in theme but not identical in target or timing.

## Final State at End of Test

At the conclusion of testing, the backend state was:

- Docker image build: successful after build-path adjustment
- API container startup: successful
- Health endpoint: successful
- `/search` request handling: reached backend route successfully
- Outbound HTTPS request to Azure Search: failed at TLS verification stage
- Search results for `structure of barcode`: not returned

## Final Conclusion

The backend itself is operational in Docker, but the runtime environment inside the Linux container does not trust the certificate chain required for outbound TLS communication with Azure AI Search.

This is an environment-level certificate trust issue, not a FastAPI routing issue, not a query construction issue, and not an Azure Search authentication issue.

The precise failure is the container's inability to validate the remote certificate issuer chain during the HTTPS connection used by the Azure Search SDK.