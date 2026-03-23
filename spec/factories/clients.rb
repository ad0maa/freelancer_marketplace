FactoryBot.define do
  factory :client do
    name { Faker::Name.full_name }
    email { Faker::Internet.email }
  end
end
