// GET /api/status -> what this deployment can do. The page hides what it can't.
import {json} from "../_lib/http.js";

export function onRequestGet({env}) {
  return json({public_mode: true, audio: false, fingerprint: null, youtube_api: !!env.YOUTUBE_API_KEY,
    edmtrain: false, ticketmaster: false, shows: false});
}
