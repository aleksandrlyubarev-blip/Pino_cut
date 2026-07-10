# Grok Image Consistency Guide — Multi-Shot Cinematic Work

How PinoCut engineers character/style consistency across a generated shot sequence when the model (Grok image generation, and Flux-class models generally) offers strong prompt adherence but **no user-facing seed control**. Consistency here is a prompt-architecture problem, not a settings problem.

## The honest constraint, first

- Grok does not expose seeds or fixed latents. Two identical prompts produce similar-but-not-identical results. **Pixel-exact character identity across shots is not achievable by prompting alone.**
- What IS achievable: recognizably-the-same character/world at normal viewing distance and cut pace, which is what film needs. The techniques below stack to get there, and the shot-planning workarounds hide the residual drift.

## Technique 1 — Verbatim anchor blocks (highest leverage)

Every entity that must stay consistent gets one **frozen descriptor block**, written once and reused verbatim — never paraphrased, never reordered — in every prompt where it appears.

Character anchor example:

```
MARA: woman in her mid-30s, sharp jawline, short black asymmetric bob with
blunt fringe, pale skin, dark brown eyes, small scar through left eyebrow,
wearing a charcoal wool coat with a high collar over a matte black turtleneck
```

Why verbatim matters: image models bind attributes to token patterns. "Short black bob" and "black short bob haircut" land on different neighborhoods. Paraphrase = re-roll.

Same for the **style bible** — one block prepended to every prompt in the project:

```
STYLE: cinematic still, shot on anamorphic 40mm, shallow depth of field,
Kodak 2383 print film emulation, teal shadows and warm amber highlights,
volumetric haze, high contrast low-key lighting, film grain, 1:1 aspect
```

Prompt composition is always: `[STYLE block] + [CHARACTER block(s)] + [shot-specific: framing, action, camera angle] + [avoid list]`. Only the shot-specific part changes.

## Technique 2 — Character sheets before any story shot

Before generating shot 1, generate a **character sheet session**: front, 3/4, profile, plus 2–3 key expressions, all with the anchor block. Purpose:

1. Validates the anchor block — if the model drifts across 5 sheet images, tighten the block (add distinguishing features: scar, specific hairline, defined garment) before wasting shot generations.
2. The best sheet images become **reference images** where image-input conditioning is available (Technique 3).
3. The sheet is the QC reference: every story shot is compared against it before acceptance.

Distinguishing features are anchors' best friends: models hold a "scar through left eyebrow" or "asymmetric bob" far more reliably than a generically pretty face.

## Technique 3 — Reference-image chaining

Where the tool accepts image input (Grok's image-edit / image-to-image paths), feed the character sheet as reference and prompt only the delta: "same woman, now seen from behind at a rain-lit window, medium shot." Chain rule: **always reference the canonical sheet, not the previous shot** — chaining shot-to-shot compounds drift like a photocopy of a photocopy.

## Technique 4 — Scene-level lighting lock

Consistency breaks most visibly when lighting logic changes between shots of one scene. Fix a **lighting recipe per scene** in the style of a gaffer's note, and reuse it verbatim within the scene:

```
LIGHTING (Scene 2): single cool key from window camera-left, warm practical
lamp fill camera-right, deep shadows, night interior
```

Change blocking and framing per shot; never change the light within a scene.

## Technique 5 — Batch by setting, iterate takes

Generate all shots that share a setting in one working session, in shot-list order. Generate 3–4 takes per shot (`s04_t01..t04`), select against the character sheet, and record which take won and why in `project.json`. Drift shows up per-generation, so takes are cheap insurance; re-prompting later in a fresh session is where wholesale look-shifts happen.

## Technique 6 — Plan the edit to hide the drift

The director-level workarounds that make residual inconsistency invisible:

- **Vary shot scale.** Wide → medium → close sequences give the eye no side-by-side face comparison. Avoid consecutive same-size close-ups of a generated character.
- **High-risk framings for high-risk shots.** Back-lit silhouettes, over-shoulder, hands/detail inserts, and profile shots carry story while hiding identity drift.
- **Cutaways as consistency airbags.** Environment/prop inserts between two character shots reset the viewer's identity memory.
- **Grade unifies.** A single Resolve grade (one node tree applied across all gen shots, plus matched grain) masks minor palette/texture drift between generations — plan for it rather than chasing perfect in-model color.

## QC checklist (before any asset enters Resolve)

- [ ] Identity: same person as the character sheet at final crop size? (Check at 100%, then at 25% — the edit plays at the latter.)
- [ ] Lighting direction consistent with the scene's lighting lock?
- [ ] Generated at final aspect ratio (never crop 16:9 out of 1:1 — composition and DOF break)?
- [ ] No artifacts (hands, text, geometry) inside the area the edit features?
- [ ] Filename follows `s<NN>_t<NN>.png`, winner noted in project.json?

## Failure playbook

| Symptom | Fix |
|---|---|
| Face drifts despite anchor | Add 1–2 distinguishing features to the anchor; move to reference-image conditioning; reframe shot to 3/4 or wider |
| Style drifts across a session | Style bible block missing or paraphrased in some prompts — diff your prompts; restore verbatim |
| Wardrobe changes | Garments need their own specific adjectives ("charcoal wool coat with a high collar" not "dark coat") |
| Scene looks like different times of day | Lighting lock missing — retrofit the recipe and regenerate the outliers only |
| One shot won't converge after ~6 takes | Change the shot, not the prompt: different angle/scale on the same story beat is cheaper than take 12 |
