FactoryBot.define do
  factory :skill do
    name { Faker::Job.field }
  end
end
