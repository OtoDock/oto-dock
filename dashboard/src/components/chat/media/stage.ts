// The media stage and what is drawn over media. A photo or a video needs
// its own dark surround in both themes, and a control or a caption drawn
// over a frame must read on any frame, so these are white on black at an
// opacity whatever the theme: a palette that is not the interface's, so
// each string carries its own exemption.

/** The image viewer's backdrop. */
// eslint-disable-next-line otodock/no-raw-palette -- the media stage is dark in both themes
export const STAGE = 'bg-black/85'
/** A video's own frame, behind the picture. */
// eslint-disable-next-line otodock/no-raw-palette -- the media stage is dark in both themes
export const STAGE_FRAME = 'bg-black'
/** A control or a label on the stage. */
// eslint-disable-next-line otodock/no-raw-palette -- a control on the dark stage
export const ON_STAGE = 'bg-white/10 hover:bg-white/20 text-white'
/** Text on the stage, and its quieter line (70 percent keeps 4.5:1 there). */
// eslint-disable-next-line otodock/no-raw-palette -- text on the dark stage
export const STAGE_TEXT = 'text-white'
// eslint-disable-next-line otodock/no-raw-palette -- text on the dark stage
export const STAGE_TEXT_MUTED = 'text-white/70'
/** A control drawn over a picture (a thumbnail's buttons, a video's play). */
// eslint-disable-next-line otodock/no-raw-palette -- a control over any picture
export const OVER_MEDIA = 'bg-black/50 hover:bg-black/70 text-white'
/** The darkening of a picture under the pointer. */
// eslint-disable-next-line otodock/no-raw-palette -- a shade over any picture
export const MEDIA_HOVER = 'bg-black/0 group-hover:bg-black/10'
/** The video's control bar: a fade from the bottom of the picture. */
// eslint-disable-next-line otodock/no-raw-palette -- a shade over any picture
export const VIDEO_BAR = 'bg-linear-to-t from-black/75 to-transparent text-white'
/** The video's tracks and spinner, white on the picture. */
// eslint-disable-next-line otodock/no-raw-palette -- a track over any picture
export const VIDEO_TRACK = 'bg-white/25'
// eslint-disable-next-line otodock/no-raw-palette -- a track over any picture
export const VIDEO_BUFFERED = 'bg-white/40'
// eslint-disable-next-line otodock/no-raw-palette -- a track over any picture
export const VIDEO_VOLUME = 'bg-white/30 accent-white'
// eslint-disable-next-line otodock/no-raw-palette -- a spinner over any picture
export const VIDEO_SPINNER = 'border-white/30 border-t-white'
// eslint-disable-next-line otodock/no-raw-palette -- a slider's thumb over any picture
export const VIDEO_THUMB = '[&::-webkit-slider-thumb]:bg-white'
