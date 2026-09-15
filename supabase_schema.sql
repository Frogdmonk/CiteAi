create extension if not exists vector;

create table if not exists public.documents (
	id uuid primary key default gen_random_uuid(),
	document_id uuid,
	owner_id text,
	document_name text not null,
	page_number integer not null check (page_number > 0),
	chunk_number integer not null check (chunk_number > 0),
	content text not null,
	coordinates jsonb not null default '[]'::jsonb,
	embedding vector(384) not null
);

alter table public.documents add column if not exists document_id uuid;
alter table public.documents add column if not exists owner_id text;
alter table public.documents add column if not exists coordinates jsonb;

with document_groups as (
		select document_name, gen_random_uuid() as new_document_id
		from public.documents
		where document_id is null
		group by document_name
)
update public.documents as d
set document_id = g.new_document_id,
		owner_id = coalesce(d.owner_id, 'admin')
from document_groups as g
where d.document_name = g.document_name
	and d.document_id is null;

update public.documents
set owner_id = 'admin'
where owner_id is null;

update public.documents
set coordinates = '[]'::jsonb
where coordinates is null;

alter table public.documents alter column document_id set not null;
alter table public.documents alter column owner_id set not null;
alter table public.documents alter column coordinates set default '[]'::jsonb;
alter table public.documents alter column coordinates set not null;

create index if not exists documents_owner_idx
on public.documents (owner_id, document_id);

create index if not exists documents_embedding_idx
on public.documents using ivfflat (embedding vector_cosine_ops)
with (lists = 100);

drop function if exists public.match_documents(vector, float, integer);
drop function if exists public.match_documents(vector, float, integer, text, uuid[]);

create function public.match_documents(
	query_embedding vector(384),
	match_threshold float,
	match_count integer,
	filter_owner_id text,
	filter_document_ids uuid[]
)
returns table (
	id uuid,
	document_id uuid,
	document_name text,
	page_number integer,
	chunk_number integer,
	content text,
	coordinates jsonb,
	similarity float
)
language sql
stable
as $$
	select
		d.id,
		d.document_id,
		d.document_name,
		d.page_number,
		d.chunk_number,
		d.content,
		d.coordinates,
		1 - (d.embedding <=> query_embedding) as similarity
	from public.documents as d
	where 1 - (d.embedding <=> query_embedding) >= match_threshold
	  and (filter_owner_id is null or d.owner_id = filter_owner_id)
	  and (cardinality(filter_document_ids) = 0 or d.document_id = any(filter_document_ids))
	order by d.embedding <=> query_embedding
	limit match_count;
$$;

alter table public.documents enable row level security;

drop policy if exists "CiteAI can read documents" on public.documents;
drop policy if exists "CiteAI can insert documents" on public.documents;

create policy "CiteAI can read documents"
on public.documents for select
using (true);

create policy "CiteAI can insert documents"
on public.documents for insert
with check (true);

drop policy if exists "CiteAI can delete documents" on public.documents;
create policy "CiteAI can delete documents"
on public.documents for delete
using (true);
