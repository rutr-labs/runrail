/** A failed request, with the status kept so a caller can tell "gone" (404)
 *  from "could not reach the server" and act differently. */
export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The one line to show for an error body. FastAPI's validation errors are a
 *  list of {loc, msg}, which stringified reads "[object Object]". */
export function errorMessage(body: string, status: number): string {
  let detail: unknown = body;
  try { detail = JSON.parse(body).detail ?? body; } catch { /* response was not JSON */ }
  if (Array.isArray(detail)) {
    const lines = detail.map((d: { loc?: unknown[]; msg?: string }) => {
      const where = (d.loc ?? []).filter(p => p !== 'body').join('.');
      return where ? `${where}: ${d.msg ?? ''}` : d.msg ?? JSON.stringify(d);
    });
    return lines.join('; ') || `Request failed: ${status}`;
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return String(detail ?? '') || `Request failed: ${status}`;
}

export async function api<T>(path:string, options?:RequestInit):Promise<T>{
  const response=await fetch(`/api${path}`,{headers:{'Content-Type':'application/json'},...options})
  if(!response.ok){
    throw new ApiError(errorMessage(await response.text(), response.status), response.status)
  }
  return response.status===204?undefined as T:response.json()
}
export const post=<T>(path:string,data:unknown)=>api<T>(path,{method:'POST',body:JSON.stringify(data)})
export const put=<T>(path:string,data:unknown)=>api<T>(path,{method:'PUT',body:JSON.stringify(data)})
export const del=(path:string)=>api<void>(path,{method:'DELETE'})
