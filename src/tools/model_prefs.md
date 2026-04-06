# Model Preferences

Set the main and image models explicitly, with no hidden heuristics.

Simplicity rules:
- One call sets what is used.
- No automatic switching behind the scenes.
- If a model is unsupported, fail loudly.

## set_model_prefs

Update the per-user model choices.

**Parameters:**
- `main_model` (string, optional): Model used for normal text tasks
- `image_model` (string, optional): Model used for image understanding

**Behavior:**
- Only updates fields you pass.
- Rejects unknown models with a clear error.
- Defaults to automatic best-model selection when nothing is set.
