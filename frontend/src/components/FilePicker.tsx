import {useEffect,useState} from 'react';
import {api} from '../api';

type Listing={root:string;path:string;parent:string|null;entries:{name:string;path:string;is_directory:boolean}[]};

export function FilePicker({value,onChange,mode='files',label,placeholder,browseFrom}:{value:string;onChange:(value:string)=>void;mode?:'files'|'directories';label:string;placeholder:string;browseFrom?:string}){
  const[open,setOpen]=useState(false);
  return <label className="field"><span>{label}</span><div className="path-input"><input value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder}/><button type="button" className="icon-button" onClick={()=>setOpen(true)} aria-label={`Browse for ${label}`}>⌘</button></div>{open&&<Browser mode={mode} initial={value} browseFrom={browseFrom} onClose={()=>setOpen(false)} onSelect={path=>{onChange(path);setOpen(false)}}/>}</label>
}

function Browser({mode,initial,browseFrom,onClose,onSelect}:{mode:'files'|'directories';initial:string;browseFrom?:string;onClose:()=>void;onSelect:(path:string)=>void}){
  const[data,setData]=useState<Listing>();const[error,setError]=useState('');const[selected,setSelected]=useState(initial);
  const load=(path?:string)=>{setError('');api<Listing>(`/filesystem?mode=${mode}${path?`&path=${encodeURIComponent(path)}`:''}`).then(result=>{setData(result);setSelected(mode==='directories'?result.path:'')}).catch(e=>setError(e.message))};
  useEffect(()=>{const separator=Math.max(initial.lastIndexOf('/'),initial.lastIndexOf('\\'));const dir=browseFrom||(separator>=0?(mode==='directories'?initial:initial.slice(0,separator)):undefined);if(dir){setError('');api<Listing>(`/filesystem?mode=${mode}&path=${encodeURIComponent(dir)}`).then(result=>{setData(result);setSelected(mode==='directories'&&initial?initial:'')}).catch(()=>load(undefined))}else{load(undefined)}},[]);
  // Capture phase: the host Modal's document listener registered earlier and
  // would otherwise see Escape first and close the whole form under us.
  useEffect(()=>{const onKey=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();onClose()}};document.addEventListener('keydown',onKey,true);return()=>document.removeEventListener('keydown',onKey,true)},[onClose]);
  return (
    <div className="modal-shade" onMouseDown={e=>{if(e.target===e.currentTarget)onClose()}}>
      <section className="modal file-browser" role="dialog" aria-modal="true">
        <div className="modal-head">
          <div>
            <h3 className="modal-title">Choose {mode==='directories'?'a folder':'a file'}</h3>
            <p className="modal-subtitle">Browsing paths available to this RunRail server</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose}>×</button>
        </div>
        <div className="crumbs">
          <button type="button" onClick={()=>load()}>Home</button>
          <span>›</span>
          <code>{data?.path||'Loading…'}</code>
        </div>
        {error
          ? <div className="empty error">Unable to open this location</div>
          : <div className="file-list">
              {data?.parent && (
                <button type="button" className="file-row" onClick={()=>load(data.parent!)}>
                  <span className="file-icon">↖</span>
                  <span><strong>Go up</strong><small>{data.parent}</small></span>
                </button>
              )}
              {data?.entries.map(item=>(
                <button
                  type="button"
                  className={`file-row${selected===item.path?' selected':''}`}
                  key={item.path}
                  onDoubleClick={()=>item.is_directory?load(item.path):onSelect(item.path)}
                  onClick={()=>item.is_directory&&mode==='directories'?setSelected(item.path):item.is_directory?load(item.path):setSelected(item.path)}
                >
                  <span className="file-icon">{item.is_directory?'▰':'▤'}</span>
                  <span><strong>{item.name}</strong><small>{item.is_directory?'Folder':'File'}</small></span>
                  {item.is_directory&&<b>›</b>}
                </button>
              ))}
            </div>
        }
        <div className="modal-foot">
          <span className="selected-path">{selected||'Select an item'}</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn-primary btn-sm" disabled={!selected} onClick={()=>onSelect(selected)}>Choose</button>
        </div>
      </section>
    </div>
  );
}
