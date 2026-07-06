export async function api<T>(path:string, options?:RequestInit):Promise<T>{
  const response=await fetch(`/api${path}`,{headers:{'Content-Type':'application/json'},...options})
  if(!response.ok){
    const body=await response.text()
    let message=body
    try{message=JSON.parse(body).detail||body}catch{/* response was not JSON */}
    throw new Error(message||`Request failed: ${response.status}`)
  }
  return response.status===204?undefined as T:response.json()
}
export const post=<T>(path:string,data:unknown)=>api<T>(path,{method:'POST',body:JSON.stringify(data)})
export const put=<T>(path:string,data:unknown)=>api<T>(path,{method:'PUT',body:JSON.stringify(data)})
export const del=(path:string)=>api<void>(path,{method:'DELETE'})
