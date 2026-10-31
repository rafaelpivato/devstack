/** Explicit HTTP(S) bookmarks with one optional workstation host placeholder.
 * This never probes Docker, validates port mappings, or guesses a URL.
 */
export function resolveApplicationUrl(configured:string, workstationHost:string):string|null {
  if(!configured || configured.length>2048 || configured.trim()!==configured ||
      /[\\\u0000-\u0020\u007f]/.test(configured))return null;
  const usesHost=configured.includes("{host}");
  if(usesHost && (configured.split("{host}").length!==2 ||
      !/^[a-z0-9][a-z0-9.-]*$/i.test(workstationHost)))return null;
  const raw=usesHost?configured.replace("{host}",workstationHost):configured;
  if(/[{}]/.test(raw))return null;
  try {
    const url=new URL(raw);
    if(!["http:","https:"].includes(url.protocol)||!url.hostname||
        url.username||url.password||url.hash||url.search||
        (usesHost&&url.hostname!==workstationHost.toLowerCase()))return null;
    return url.href;
  } catch{return null;}
}
