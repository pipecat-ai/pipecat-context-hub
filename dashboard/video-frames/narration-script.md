# Latent Space Explorer — 10s Narration Script

## Frames & Timestamps

| Time | Frame | Visual | Narration (Kokoro TTS) |
|------|-------|--------|------------------------|
| 0:00–0:02 | `01-clusters-overview.png` | 200 K-means clusters, all content types visible. Blue=docs, green=code, purple=source. | "Sixteen thousand chunks from twelve Pipecat repos, projected from three-hundred-eighty-four dimensions down to three." |
| 0:02–0:04 | `02-rotated-angle.png` | Slight rotation revealing depth and cluster separation. | "Each cluster blends colours by content type — blue for docs, green for examples, purple for framework source." |
| 0:04–0:05 | `03-docs-only.png` | Docs-only filter: code and source unchecked, only blue clusters remain. | "Filter to docs only — see how documentation occupies its own region of the embedding space." |
| 0:05–0:07 | `04-all-points.png` | All 16,284 individual points loaded, dense core with outlier satellites. | "Expand to all sixteen thousand points. The dense core is where concepts overlap across all three content types." |
| 0:07–0:08 | `05-search-transport.png` | Search for "transport" — 1,610 matches highlighted white against dimmed background. | "Search for transport — sixteen hundred matches light up, spanning docs, examples, and API source." |
| 0:08–0:10 | `06-cluster-expanded-labels.png` | Expanded cluster #140 with labels: SOXRStreamAudioResampler, RNNoiseFilter, DailyTransportClient, VADAnalyzer. | "Zoom into a cluster — audio processing classes like resampler, noise filter, and VAD sit right next to each other. Semantic neighbours in code are spatial neighbours here." |

## Production Notes

- **Frame rate**: 2 seconds per frame for frames 1, 2, 4; 1 second for frames 3, 5; 2 seconds for frame 6. Total ~10s.
- **Transitions**: Cross-dissolve between frames (0.3s each) for smooth flow.
- **TTS voice**: Kokoro TTS, normal pace. Each narration segment should align with its frame duration.
- **Audio**: No background music, clean voiceover only.
- **Resolution**: All frames 1024x1024px (Playwright default viewport).

## Assembly Command (ffmpeg example)

```bash
# Create video from frames with crossfade transitions
# Step 1: Generate TTS audio for each segment using Kokoro
# Step 2: Concatenate with ffmpeg

ffmpeg -loop 1 -t 2 -i 01-clusters-overview.png \
       -loop 1 -t 2 -i 02-rotated-angle.png \
       -loop 1 -t 1 -i 03-docs-only.png \
       -loop 1 -t 2 -i 04-all-points.png \
       -loop 1 -t 1 -i 05-search-transport.png \
       -loop 1 -t 2 -i 06-cluster-expanded-labels.png \
       -filter_complex "[0:v][1:v]xfade=transition=fade:duration=0.3:offset=1.7[v01]; \
                         [v01][2:v]xfade=transition=fade:duration=0.3:offset=3.4[v02]; \
                         [v02][3:v]xfade=transition=fade:duration=0.3:offset=4.1[v03]; \
                         [v03][4:v]xfade=transition=fade:duration=0.3:offset=5.8[v04]; \
                         [v04][5:v]xfade=transition=fade:duration=0.3:offset=6.5[v05]" \
       -map "[v05]" -pix_fmt yuv420p -c:v libx264 latent-space-tour.mp4
```
