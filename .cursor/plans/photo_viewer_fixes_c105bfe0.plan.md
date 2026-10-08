---
name: Photo viewer fixes
overview: Add a large photo view in the Ajax photos page, post a Home Assistant notification with the picture when a new photo is saved, check the hub log more often, and restore the phone menu button.
todos:
  - id: lightbox
    content: Open a full-screen photo view from a thumbnail tap, with Close and Download
    status: completed
  - id: notify-faster
    content: Poll page 1 every 5 seconds, refresh the open gallery, and post a signed-image notification per new burst
    status: completed
  - id: phone-menu
    content: Add the phone menu button and stop unauthenticated image requests
    status: completed
isProject: false
---

# hPhoto viewer, notifications, and the phone menu

All of this stays in [custom_components/ajax/image.py](custom_components/ajax/image.py). Sensors, arming, and Video Edge cameras stay as they are.

## 1. Large view when a photo is tapped

In the sidebar panel script, a tap on a picture opens a full-screen layer over the gallery: the same JPEG, larger, with Close and Download. Tapping the dark area around it, or Close, dismisses it. The Download button under the thumbnail keeps working and does not open the large view.

## 2. A notification for each new photo, and a faster gallery

New pictures wait on two timers today: the hub log is only fetched every 15 seconds, and a 20 second cache skips most of those fetches. After the first backfill, page 1 is checked every **5 seconds**, and that cache drops to **5 seconds**. Older log pages are still read once. The in-progress retry stays, so a burst that is still transferring is picked up on the next 5 second check instead of the next half minute.

The panel reloads its list every 5 seconds and adds only pictures that were not there yet, so an open gallery fills in without a manual refresh.

When a burst is saved after startup (not the photos already on disk at startup), one entry is added to the Home Assistant notifications drawer, the panel circled in your screenshot. The title is the MotionCam name, the message includes the picture, and the image URL is a short-lived signed link (`async_sign_path`, about 12 hours) so the phone can show it without a login failure. A link back to Ajax photos is included. The standing “where to find photos” notice is not posted again.

## 3. Phone menu button

On a phone the window is narrow, so Home Assistant hides the sidebar and only shows it when the panel draws `ha-menu-button`. iPad and iMac stay wide, so the sidebar is already on screen and that button stays hidden. The Ajax photos panel never draws the button, which is why the menu control disappears only on the phone.

The panel gets a top bar with `ha-menu-button` and the title. Scrolling stays on the photo list under that bar, not on a full-screen layer, so the button is not covered. Image requests use Home Assistant’s authenticated fetch only, so a phone does not send a bad token and raise “Login attempt failed”.

The panel script is served with `Cache-Control: no-cache`, so the phone picks up this script after a reload instead of keeping the old one. This issue is not there in the desktop site mode but on the mobile site mode and the home assistant sidebar issue on mobile is only present in the ajax photos tab and not on other parts of the home assistant app. What i mean is if i open the devices and integration tab or any other tab in my home assistant app on mobile phone the sidebar is showing up but only in ajax photos tab on mobile it is not showing up.

Besides these 3 changes don't change any other thing in the codebase.
