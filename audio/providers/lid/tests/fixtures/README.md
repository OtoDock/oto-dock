# Test fixtures

| File | What | Origin |
|---|---|---|
| `greek-8k.wav` | 5.2 s of Greek speech, 8 kHz mono PCM16, the line rate the tracker upsamples: a synthetic voice reading one sentence written for this test ("Καλημέρα σας. Θα ήθελα να κλείσω ένα ραντεβού για αύριο το πρωί, αν γίνεται γύρω στις δέκα. Ευχαριστώ πολύ."). Synthesized with ElevenLabs text-to-speech at 16 kHz, low-passed at 3.4 kHz and decimated to 8 kHz here. No person's voice. | OtoDock |

The offline test (`test_ecapa_offline.py`) checks that the vendored identifier
names this clip Greek at one, two and three seconds. A clip that replaces it
must be a real 8 kHz line recording or a resample of one, never a 16 kHz
file renamed.
