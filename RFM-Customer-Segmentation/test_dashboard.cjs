const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={}; const initial={population:'primary',channel:'',cohort:'',segment:'',min:'0',max:'731',catsegment:'',definition:'Primary'};
function el(id){return elements[id]??={value:initial[id]??'',checked:true,innerHTML:'',addEventListener(){},appendChild(){},click(){}}}
let downloadedBlob,downloadedLink;
const document={getElementById:el,querySelectorAll:()=>Object.values(elements),querySelector:()=>el('main'),createElement:()=>({innerHTML:'',click(){downloadedLink=this}})};
const downloadURL={createObjectURL(blob){downloadedBlob=blob;return 'blob:test-download'},revokeObjectURL(){}};
const context={document,window:{},console,Blob,URL:downloadURL,setTimeout(fn){fn()}};vm.createContext(context);
const html=fs.readFileSync(__dirname+'/dashboard.html','utf8');const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];vm.runInContext(code,context);
const api=context.window.rfmTest,results=[];function test(name,fn){fn();results.push({check:name,passed:true})}
test('Primary default 1892',()=>assert.equal(api.filtered().length,1892));
test('Primary value reconciliation',()=>assert(Math.abs(api.filtered().reduce((s,x)=>s+x.monetary,0)-1454282.7)<1e-6));
test('Inclusive exact recency boundary',()=>{el('min').value='32';el('max').value='32';assert(api.filtered().length>0);assert(api.filtered().every(x=>x.recency===32))});
test('Empty and reversed ranges',()=>{el('min').value='1000';el('max').value='999';assert.equal(api.filtered().length,0)});
test('All accounts including null recency',()=>{el('population').value='all';el('min').value='0';el('max').value='731';assert.equal(api.filtered().length,3600)});
test('Exclude null recency',()=>{el('unknown').checked=false;assert.equal(api.filtered().length,2047);el('unknown').checked=true});
test('Incomplete population',()=>{el('population').value='incomplete';assert.equal(api.filtered().length,155);assert(api.filtered().every(x=>x.monetary===null))});
test('Combined channel cohort segment',()=>{el('population').value='primary';el('channel').value='referral';el('cohort').value='2025';el('segment').value='Champions';assert(api.filtered().every(x=>x.acquisition_channel==='referral'&&x.signup_cohort==='2025'&&x.segment==='Champions'));assert(api.filtered().length>0)});
const defaultSelection=api.data.filter(x=>x.primary_eligible).map(x=>x.customer_id);
test('Reset restores all default population filters and IDs',()=>{el('unknown').checked=false;el('min').value='100';el('max').value='200';el('reset').onclick();assert.equal(el('population').value,'primary');for(const id of ['channel','cohort','segment'])assert.equal(el(id).value,'');assert.equal(Number(el('min').value),0);assert.equal(Number(el('max').value),731);assert.equal(el('unknown').checked,true);assert.deepEqual(Array.from(api.filtered(),x=>x.customer_id),defaultSelection)});
test('Empty rendering has no NaN',()=>{el('min').value='1000';el('max').value='999';api.update();assert(!el('summary').innerHTML.includes('NaN'))});
test('Share label specifies selected accounts',()=>{el('reset').onclick();assert(el('summary').innerHTML.includes('<th>Share of selected accounts</th>'))});
test('CRM table contains every segment action and success metric',()=>{const section=html.split('<h2>Review interpretation</h2>')[1].split('</section>')[0];assert(section.includes('<th>Recommended action</th><th>Success metric</th>'));for(const segment of ['Champions','Loyal','At risk','Lapsed low-frequency','Recent low-frequency','Developing'])assert(section.includes('<td>'+segment+'</td>'));assert(section.includes('90-day second-order conversion'))});
test('Currency formatting always has two decimal places',()=>{assert.equal(vm.runInContext('money(12)',context),'$12.00');assert.equal(vm.runInContext('money(12.3)',context),'$12.30');assert.equal(vm.runInContext('money(0)',context),'$0.00');for(const node of Object.values(elements)){for(const match of node.innerHTML.matchAll(/\$[\d,.]+/g))assert(/^\$[\d,]+\.\d{2}$/.test(match[0]),match[0])}});
async function verifyDownload(name){
 const selected=api.filtered();el('download').onclick();assert.equal(downloadedLink.download,'rfm_review_filtered.csv');assert.equal(downloadedLink.href,'blob:test-download');
 const csv=await downloadedBlob.text(),lines=csv.replace(/\n$/,'').split('\n'),keys=lines[0].split(',');assert.equal(lines.length-1,selected.length);
 const expected=selected.map(x=>keys.map(k=>'"'+String(x[k]??'').replaceAll('"','""')+'"').join(','));assert.deepEqual(lines.slice(1),Array.from(expected));results.push({check:name,passed:true});
}
(async()=>{
 el('population').value='primary';el('channel').value='referral';el('cohort').value='2025';el('segment').value='Champions';el('min').value='0';el('max').value='88';assert(api.filtered().length>0);await verifyDownload('CSV bytes match active combined selection and original numeric values');
 el('reset').onclick();await verifyDownload('CSV after reset matches all 1892 default customers');
 el('population').value='incomplete';await verifyDownload('Incomplete-price CSV preserves null monetary and partial spend');
 el('min').value='1000';el('max').value='999';await verifyDownload('Empty selection exports header only');
 fs.writeFileSync(__dirname+'/output/dashboard_validation.json',JSON.stringify({method:'Node VM with DOM stub executing generated dashboard code and inspecting the actual download Blob; not a browser visual test',checks:results},null,2));console.log(JSON.stringify(results,null,2));
})().catch(error=>{console.error(error);process.exitCode=1});
