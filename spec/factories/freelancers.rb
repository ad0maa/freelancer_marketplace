FactoryBot.define do
  factory :freelancer do
    name { Faker::Name.full_name }
    email { Faker::Internet.email }
    hourly_rate { Faker::Number.decimal(l_digits: 2, r_digits: 2) }
    availability { true }
  end
end
