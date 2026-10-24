# Local Image Studio Design

## Goal

Build a polished local web interface for calling configurable OpenAI-compatible image generation services. Users can enter an API base URL, API key, model, and prompt, optionally provide a reference image, inspect generated images, and download them.

## User workflow

1. Start the local Python application and open its browser interface.
2. Configure the API base URL, API key, and model. Keep the key in local application configuration and never include it in generation history or browser-delivered source code.
3. Enter a prompt, optionally upload a reference image, and choose whether the response contains Markdown/Base64 image data that should be converted to an image.
4. Generate an image and view it in a result gallery. Open a larger preview, inspect/copy the returned Base64 when available, or download the image.
5. The application periodically removes generated images that have not been downloaded and are at least 72 hours old. Downloaded images remain in the local gallery.

## Approaches considered

- **Python local web service with a browser UI (recommended):** one local process can make API requests, protect credentials from page source, parse response payloads, manage files, and run expiration cleanup. It is easy to launch and does not require a desktop packaging pipeline.
- **Browser-only application:** simpler deployment, but handling provider-specific responses and safely storing API keys is less suitable, and cleanup would depend on an open browser tab.
- **Packaged desktop application:** offers a standalone window but adds packaging and update complexity without improving the core workflow enough to justify it initially.

## Architecture

- A small Python web server serves the UI and owns API calls, local settings, image files, download state, and cleanup.
- A responsive single-page interface provides configuration, generation controls, and a gallery beside the controls on wide screens; it stacks cleanly on narrow screens.
- Store generated files and a small metadata index under an application data directory. Keep settings, including the API key, in a separate local-only configuration file with restrictive permissions where supported.
- Keep the implementation focused: one generation flow, one response parser, and one image gallery. Avoid accounts, remote storage, or multi-user features.

## API request and response handling

- Use an OpenAI-compatible image generation request matching the supplied Python example, with configurable base URL and model.
- Support an optional reference image through the compatible image-edit request path. If a provider rejects the request or does not support image edits, show the returned error in the interface.
- Add a checkbox labeled along the lines of “Convert Markdown/Base64 response to image.” When enabled, recognize Markdown image data URLs such as `![image](data:image/png;base64,...)`, direct data URLs, and the common `data[].b64_json` response field; decode and save valid images. When disabled, handle direct image responses and standard structured image results without trying to interpret arbitrary text as an image.
- Validate decoded bytes and allowed image types before saving. Show a readable error for unsupported or malformed responses rather than silently creating a broken image.
- Show the image preview and allow inspection/copying of Base64 when it exists in the response.

## Image lifecycle

- Record creation time, local path, media type, and whether the image has been downloaded.
- Mark an image downloaded when the application serves a successful download response.
- A cleanup task runs at startup and periodically while the service runs. It removes only not-yet-downloaded generated images older than 72 hours, along with their metadata.
- Downloaded files remain until the user removes them manually. The gallery provides a manual delete action for any item.

## Errors and security

- Keep API calls on the Python server so the key is not exposed in client code.
- Do not log API keys or full authorization headers. Provide clear errors for connection failures, API errors, invalid response formats, and unsupported reference-image operations.
- Bind the server to localhost by default. Restrict file access to the application data directory and use generated filenames rather than user-supplied paths.
- Treat provider response text and uploaded image filenames as untrusted input.

## Initial acceptance checks

- A user can save local API configuration and generate an image through an OpenAI-compatible endpoint.
- Both a standard Base64 image result and a Markdown embedded data URL can be decoded and shown when conversion is enabled.
- A direct image result can be shown without enabling conversion.
- Reference image upload is sent through the image-edit path and provider errors are visible.
- Generated images can be enlarged, downloaded, manually deleted, and preserved across application restarts.
- An undownloaded image older than 72 hours is removed by cleanup; a downloaded image is retained.

## Scope limits

Arbitrary website-specific request schemas, account management, remote hosting, and multi-user access are out of scope for the initial version. The first version targets the OpenAI-compatible API shape shown in the supplied example and common image response forms described above.
