-- mart_jobs.sql
-- Unified jobs for the app: ONE view over both raw sources, plus the education rules it uses.
-- Run the WHOLE file in the Supabase SQL Editor. Safe to run again:
--   * the view is dropped and rebuilt
--   * the rules table keeps any edits you made (new rules are only added, never overwritten)
--
-- Only columns that exist in BOTH sources are kept:
--   column                  punjab (raw.punjab_jobs_portal)         njp (raw.njp_jobs)
--   ----------------------  --------------------------------------  ---------------------------------
--   job_id                  'punjab:' || id                         'njp:' || id
--   source                  'punjab'                                'njp'
--   source_job_id           id (url slug)                           id (job number)
--   title                   title                                   title
--   employer                project                                 employer
--   description             description                             description
--   employment_type         employment_status                       job_type
--   grade                   level  ('N/A' -> null)                  grade
--   vacancies               total_position                          vacancies
--   experience_years        smallest value in years_of_experience   experience_years
--   education_level_years   education_level_years (site number)     DERIVED from degree names (rules below)
--   age_min, age_max        age_min, age_max                        age_min, age_max
--   salary_min, _max        salary_min, salary_max                  null (NJP does not publish it)
--   qualifications          degree_area (subject areas)             qualifications (degree names)
--   job_posted              job_posted                              job_posted
--   last_date_to_apply      last_date_to_apply                      last_date_to_apply
--   job_url                 built from the slug                     url
--   is_active               is_active                               is_active
--   scraped_at              scraped_at                              scraped_at
--   is_open (computed)      active AND deadline not passed (Pakistan date)

create schema if not exists mart;

-- =====================================================================================
-- 1. DEGREE RULES: degree name -> years of education (NJP lists names, not years)
--    First matching rule (lowest rank) wins for each degree name. Patterns are
--    case-insensitive regular expressions, matched as WHOLE WORDS (so "BS" does not
--    match inside "BSc"). years = NULL means "recognised, but do not count it".
--    Edit a rule:  update mart.degree_rules set years = 17 where rank = 45;
--    Add a rule:   insert into mart.degree_rules values (55, 'B\.?Pharm', 16, 'note');
-- =====================================================================================
create table if not exists mart.degree_rules (
  rank    int  primary key,
  pattern text not null,
  years   int,
  note    text
);
alter table mart.degree_rules enable row level security;      -- private, like the raw tables

insert into mart.degree_rules (rank, pattern, years, note) values
 (10,  'CA\)?\s*-\s*Intermediate|Chartered Accountancy\s*\(?CA\)?\s*-?\s*Intermediate',
       null, 'CA-Intermediate stage: not counted (and NOT treated as Intermediate = 12)'),
 (20,  'PhD|Ph\.D\.?|Doctorate|D\.Phil|Doctor of Philosophy',
       21,   'Doctorate'),
 (30,  'MS|M\.S\.?|MPhil|M\.Phil\.?|Master of Philosophy|Masters? in Philosophy|LLM|LL\.M\.?|Master of Laws',
       18,   'MS / MPhil / LLM'),
 (40,  'Hons\.?|Honou?rs',
       16,   'any degree with Honours = 4-year degree (matches the Punjab portal: "Bachelors (Hons) = 16 years")'),
 (44,  'B\.?\s?Sc\.?.{0,20}Engineering|B\.?\s?Sc\.?\s+Engg\.?',
       16,   'BSc Engineering = 4-year degree'),
 (45,  'BS|B\.S\.?|BSCS|BSIT|BBA|BE|B\.E\.?|B\.?\s?Tech|B\.?\s?Eng|B\.?\s?Arch|B\.?\s?Pharm|MBBS|BDS|Pharm-?D|DVM|LLB|LL\.B\.?',
       16,   'BS and other 4-year / professional degrees (MBBS, PharmD, LLB are 5-year; kept at 16, change if you want 17)'),
 (50,  'Masters?|MSc|M\.Sc\.?|MA|M\.A\.?|MCom|M\.Com\.?|MCS|MIT|MBA|M\.B\.A\.?|MEd|M\.Ed\.?|MPA|ACCA|ACMA|CA|Chartered Accountan(cy|t)|Cost and Management Accountan(cy|t)',
       16,   'Masters (2-year) and accounting bodies (ACCA, ACMA, CA)'),
 (60,  'Bachelors?|B\.?\s?Sc\.?|B\.?\s?A\.?|B\.?\s?Com\.?|ADP|ADE|Associate Degree',
       14,   'plain Bachelor / BA / BSc / BCom (2-year) and Associate Degree'),
 (70,  'DAE|D\.A\.E\.?|Diploma of Associate Engineer(ing)?',
       12,   'DAE = 12, the same value the Punjab portal uses'),
 (80,  'FSc|F\.Sc\.?|FA|F\.A\.?|ICS|ICom|I\.Com\.?|HSSC|Intermediate|Higher Secondary|A[- ]?Levels?',
       12,   'Intermediate'),
 (90,  'Matric|Matriculation|SSC|Secondary School|O[- ]?Levels?',
       10,   'Matriculation'),
 (100, 'Middle',  8, 'added: Middle (8th class)'),
 (110, 'Primary', 5, 'added: Primary')
on conflict (rank) do nothing;

-- degree names -> years. Takes the SMALLEST: the list holds alternatives, the lowest one is enough to apply.
-- Names that match no rule are ignored; if nothing matches the result is null (= unknown).
create or replace function mart.parse_education_years(quals text[])
returns int
language sql
stable
as $$
  select min(r.years)
  from unnest(quals) as q(txt)
  cross join lateral (
    select d.years
    from mart.degree_rules d
    where q.txt ~* ('(^|[^[:alnum:]])(' || d.pattern || ')($|[^[:alnum:]])')
    order by d.rank
    limit 1
  ) r
$$;

-- review list: NJP degree names that NO rule recognises yet (add a rule for the common ones)
create or replace view mart.unmapped_qualifications as
select q.txt as qualification, count(*) as jobs
from raw.njp_jobs n
cross join lateral unnest(n.qualifications) as q(txt)
where n.is_active
  and not exists (
    select 1 from mart.degree_rules d
    where q.txt ~* ('(^|[^[:alnum:]])(' || d.pattern || ')($|[^[:alnum:]])'))
group by q.txt
order by jobs desc, q.txt;

-- =====================================================================================
-- 2. THE UNIFIED VIEW
-- =====================================================================================
drop view if exists mart.jobs;

create view mart.jobs as
select
  u.*,
  (u.is_active
   and (u.last_date_to_apply is null
        or u.last_date_to_apply >= (now() at time zone 'Asia/Karachi')::date)) as is_open
from (
  select
    'punjab:' || p.id                                              as job_id,
    'punjab'::text                                                 as source,
    p.id                                                           as source_job_id,
    p.title                                                        as title,
    p.project                                                      as employer,
    p.description                                                  as description,
    p.employment_status                                            as employment_type,
    case when p.level ilike '%n/a%' then null else p.level end     as grade,
    p.total_position                                               as vacancies,
    -- years_of_experience looks like [{"16": 10}, {"12": 15}]; keep the smallest requirement
    (select min(v.value::int)
       from jsonb_array_elements(coalesce(p.years_of_experience, '[]'::jsonb)) as e,
            jsonb_each_text(case when jsonb_typeof(e) = 'object' then e else '{}'::jsonb end) as v
    )                                                              as experience_years,
    p.education_level_years                                        as education_level_years,
    p.age_min                                                      as age_min,
    p.age_max                                                      as age_max,
    p.salary_min                                                   as salary_min,
    p.salary_max                                                   as salary_max,
    p.degree_area                                                  as qualifications,
    p.job_posted                                                   as job_posted,
    p.last_date_to_apply                                           as last_date_to_apply,
    'https://jobs.punjab.gov.pk/new_recruit/job_detail/' || p.id   as job_url,
    p.is_active                                                    as is_active,
    p.scraped_at                                                   as scraped_at
  from raw.punjab_jobs_portal p

  union all

  select
    'njp:' || n.id,
    'njp'::text,
    n.id,
    n.title,
    n.employer,
    n.description,
    n.job_type,
    n.grade,
    n.vacancies,
    n.experience_years,
    mart.parse_education_years(n.qualifications),
    n.age_min,
    n.age_max,
    null::int,
    null::int,
    n.qualifications,
    n.job_posted,
    n.last_date_to_apply,
    n.url,
    n.is_active,
    n.scraped_at
  from raw.njp_jobs n
) u;

-- the scraper side (secret key = service_role) can read it
grant usage on schema mart to service_role;
grant select on mart.jobs to service_role;
grant select on mart.unmapped_qualifications to service_role;
grant select on mart.degree_rules to service_role;