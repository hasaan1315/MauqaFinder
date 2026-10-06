-- mart_jobs.sql
-- Unified jobs for the app: ONE view over both raw sources.
-- Run in the Supabase SQL Editor. Safe to run again (create or replace).
--
-- Only columns that exist in BOTH sources are kept:
--   column              punjab (raw.punjab_jobs_portal)         njp (raw.njp_jobs)
--   ------------------  --------------------------------------  --------------------
--   job_id              'punjab:' || id                         'njp:' || id
--   source              'punjab'                                'njp'
--   source_job_id       id (url slug)                           id (job number)
--   title               title                                   title
--   employer            project                                 employer
--   description         description                             description
--   employment_type     employment_status                       job_type
--   grade               level  ('N/A' -> null)                  grade
--   vacancies           total_position                          vacancies
--   experience_years    smallest value in years_of_experience   experience_years
--   age_min, age_max    age_min, age_max                        age_min, age_max
--   qualifications      degree_area (subject areas)             qualifications (degree names)
--   job_posted          job_posted                              job_posted
--   last_date_to_apply  last_date_to_apply                      last_date_to_apply
--   job_url             built from the slug                     url
--   is_active           is_active                               is_active
--   scraped_at          scraped_at                              scraped_at
--   is_open (computed)  active AND deadline not passed (Pakistan date)

create schema if not exists mart;

create or replace view mart.jobs as
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
    p.age_min                                                      as age_min,
    p.age_max                                                      as age_max,
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
    n.age_min,
    n.age_max,
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
